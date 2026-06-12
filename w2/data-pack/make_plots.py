"""Xuat cac bieu do giai thich quyet dinh cua engine ra thu muc plots/*.png.

Chay:  py make_plots.py
"""
import json
import glob
from pathlib import Path
from collections import Counter

import numpy as np
import matplotlib
matplotlib.use("Agg")          
import matplotlib.pyplot as plt

import retrieval as ret
import decision as dec
import engine as eng

OUT = Path("plots")
OUT.mkdir(exist_ok=True)

HISTORY = json.loads(Path("incidents_history.json").read_text(encoding="utf-8"))
ACTIONS = dec.load_actions("actions.yaml")

# Chay engine tren ca 8 su co, giu lai noi dung chi tiet.
runs = {}
for path in sorted(glob.glob("eval/E*.json")):
    eid = Path(path).stem
    if not eid.startswith("E"):
        continue
    runs[eid] = eng.decide(Path(path), Path("incidents_history.json"), Path("actions.yaml"))
eids = list(runs.keys())


# --- Bieu do 1: Tong quan tap du lieu ---------------------------------------
outcomes = Counter(h["outcome"] for h in HISTORY)
action_names = Counter()
for h in HISTORY:
    for a in h["actions_taken"]:
        action_names[a.split(":")[0]] += 1
fig, ax = plt.subplots(1, 2, figsize=(12, 4))
ax[0].bar(outcomes.keys(), outcomes.values(), color=["#2a9d8f", "#e9c46a", "#e76f51"])
ax[0].set_title("Phan bo ket qua lich su (n=%d)" % len(HISTORY))
ax[0].set_ylabel("so su co")
ax[1].bar(action_names.keys(), action_names.values(), color="#264653")
ax[1].set_title("Phan bo hanh dong lich su")
ax[1].tick_params(axis="x", rotation=30)
plt.tight_layout()
plt.savefig(OUT / "01_dataset_overview.png", dpi=110)
plt.close()


# --- Bieu do 2: Do tuong dong lang gieng gan nhat + nguong OOD ---------------
maxsims = [runs[e]["evidence"]["max_similarity"] for e in eids]
oods = [runs[e]["evidence"]["ood"] for e in eids]
colors = ["#e76f51" if o else "#2a9d8f" for o in oods]
plt.figure(figsize=(10, 4))
plt.bar(eids, maxsims, color=colors)
plt.axhline(0.30, color="k", ls="--", label="Nguong OOD = 0.30")
for i, s in enumerate(maxsims):
    plt.text(i, s + 0.01, f"{s:.2f}", ha="center")
plt.title("Do tuong dong lang gieng gan nhat (do = bi gan OOD)")
plt.ylabel("max_similarity")
plt.legend()
plt.tight_layout()
plt.savefig(OUT / "02_similarity_ood.png", dpi=110)
plt.close()


# --- Bieu do 3: Dong gop cua tung kenh vao diem tuong dong ------------------
chans = ["log", "trace", "service", "metric"]
M = np.array([[runs[e]["evidence"]["top_neighbors"][0]["breakdown"][c] for c in chans] for e in eids])
w = [ret.W_LOG, ret.W_TRACE, ret.W_SERVICE, ret.W_METRIC]
palette = ["#264653", "#2a9d8f", "#e9c46a", "#e76f51"]
plt.figure(figsize=(10, 4))
bottom = np.zeros(len(eids))
for j, c in enumerate(chans):
    plt.bar(eids, M[:, j] * w[j], bottom=bottom, label=f"{c} (w={w[j]})", color=palette[j])
    bottom += M[:, j] * w[j]
plt.axhline(0.30, color="k", ls="--")
plt.title("Dong gop co trong so theo tung kenh bang chung")
plt.ylabel("dong gop vao diem tuong dong")
plt.legend()
plt.tight_layout()
plt.savefig(OUT / "03_channel_breakdown.png", dpi=110)
plt.close()


# --- Bieu do 4: Bo phieu co trong so theo ket qua (E05, E06) ----------------
def vote_axis(ax, eid):
    cands = runs[eid]["evidence"]["candidate_actions"]
    names = [c["name"] for c in cands]
    scores = [c["vote_score"] for c in cands]
    cols = ["#2a9d8f" if s >= 0 else "#e76f51" for s in scores]
    ax.bar(names, scores, color=cols)
    ax.set_title(f'{eid}: bo phieu -> {runs[eid]["selected_action"]}')
    ax.set_ylabel("vote_score")
    ax.tick_params(axis="x", rotation=20)

fig, ax = plt.subplots(1, 2, figsize=(12, 4))
vote_axis(ax[0], "E05")
vote_axis(ax[1], "E06")
plt.tight_layout()
plt.savefig(OUT / "04_voting.png", dpi=110)
plt.close()


# --- Bieu do 5: Confidence vs Utility (E01) va Confidence vs Blast ----------
cands = runs["E01"]["evidence"]["candidate_actions"]
names = [c["name"] for c in cands]
conf = [c["confidence"] for c in cands]
util = [dec.utility(c["confidence"], ACTIONS.get(c["name"], {})) for c in cands]
x = range(len(names))
fig, ax = plt.subplots(1, 2, figsize=(12, 4))
ax[0].bar([i - 0.2 for i in x], conf, width=0.4, label="confidence", color="#264653")
ax[0].bar([i + 0.2 for i in x], util, width=0.4, label="utility (sau phat)", color="#2a9d8f")
ax[0].set_xticks(list(x))
ax[0].set_xticklabels(names, rotation=20)
ax[0].set_title("E01: confidence vs utility")
ax[0].legend()
for e in eids:
    m = ACTIONS.get(runs[e]["selected_action"], {})
    br = m.get("blast_radius_services", 0)
    cf = runs[e]["confidence"]
    mk = "P" if runs[e]["selected_action"] == "page_oncall" else "o"
    ax[1].scatter(br, cf, s=120, marker=mk)
    ax[1].text(br + 0.05, cf, e, fontsize=9)
ax[1].axhline(0.70, color="r", ls="--", label="cong blast cao (0.70)")
ax[1].set_xlabel("blast_radius cua hanh dong da chon")
ax[1].set_ylabel("confidence")
ax[1].set_title("Confidence vs Blast radius (P = page)")
ax[1].legend()
plt.tight_layout()
plt.savefig(OUT / "05_decision.png", dpi=110)
plt.close()


# --- Bieu do 6: Quet nguong OOD ---------------------------------------------
sweep = np.linspace(0.15, 0.45, 31)
n_ood_curve = [sum(1 for e in eids if runs[e]["evidence"]["max_similarity"] < th) for th in sweep]
plt.figure(figsize=(10, 4))
plt.step(sweep, n_ood_curve, where="mid", color="#264653")
plt.axvline(0.30, color="r", ls="--", label="nguong da chon 0.30")
plt.scatter(maxsims, [0.2] * len(eids), c=colors, zorder=5)
for e, s in zip(eids, maxsims):
    plt.text(s, 0.35, e, rotation=90, fontsize=8, ha="center")
plt.xlabel("nguong OOD")
plt.ylabel("so su co bi gan OOD")
plt.title("Quet nguong OOD - cac su co nam xa 0.30")
plt.legend()
plt.tight_layout()
plt.savefig(OUT / "06_ood_sweep.png", dpi=110)
plt.close()

print("Da xuat 6 anh vao thu muc plots/:")
for p in sorted(OUT.glob("*.png")):
    print("  ", p)
