"""Layer 3 - Cost-aware decision engine.

Turns the ranked candidate actions from Layer 2 into a single recommendation by
combining confidence with cost, blast radius, and downtime, then applying a set
of escalation gates.

Key design rules (rubric + handout):
* ``page_oncall`` is the LAST resort, never a low-cost default. It only wins when
  it is the actual outcome-weighted vote winner (neighbours paged) OR when a gate
  trips (OOD / low confidence / risky blast radius with weak confidence).
* A high blast-radius action is never auto-fired by a weakly-confident engine.
* Action parameters missing from the live incident are inferred: rollback target
  defaults to ``"previous"``; the service parameter is filled from the incident's
  root affected service.
"""
from __future__ import annotations

import yaml
from pathlib import Path


def load_actions(actions_path: str | Path) -> dict:
    """Load actions.yaml into a {name: meta} map."""
    catalog = yaml.safe_load(Path(actions_path).read_text(encoding="utf-8"))
    return {a["name"]: a for a in catalog}


# Penalty coefficients (utility is on the same 0..1 scale as confidence).
def cost_penalty(meta: dict) -> float:
    return min(meta.get("cost_min", 0) / 30.0, 1.0) * 0.15


def blast_penalty(meta: dict) -> float:
    return min(meta.get("blast_radius_services", 0) / 5.0, 1.0) * 0.25


def downtime_penalty(meta: dict) -> float:
    return min(meta.get("downtime_min", 0) / 10.0, 1.0) * 0.15


def utility(confidence: float, meta: dict) -> float:
    """utility = confidence - cost - blast - downtime penalties."""
    return round(
        confidence - cost_penalty(meta) - blast_penalty(meta) - downtime_penalty(meta),
        4,
    )


# Gating thresholds.
CONFIDENCE_FLOOR = 0.35          # below this, auto-action is not trustworthy
HIGH_BLAST = 3                   # blast radius >= this is "risky"
HIGH_BLAST_CONF = 0.70           # ...and needs at least this confidence to fire

SERVICE_PARAM_ACTIONS = {"rollback_service", "increase_pool_size", "restart_pod"}


def _build_params(action_name: str, meta: dict, root_service: str | None,
                  example_params: dict | None) -> dict:
    """Fill the action's parameter slots for the live incident.

    Service-scoped actions are re-targeted to the live root service (not the
    stale service from the historical example). Missing values get sensible
    placeholders.
    """
    example_params = example_params or {}
    declared = meta.get("params", [])
    params: dict[str, str] = {}

    if action_name in SERVICE_PARAM_ACTIONS:
        params["service"] = root_service or example_params.get("service") or "unknown-svc"

    if action_name == "rollback_service":
        params["target_version"] = "previous"
    elif action_name == "increase_pool_size":
        params["from_value"] = example_params.get("from_value", "current")
        params["to_value"] = example_params.get("to_value", "increased")
    elif action_name == "restart_pod":
        params["pod_selector"] = example_params.get("pod_selector", "default")
    elif action_name == "page_oncall":
        params["team"] = example_params.get("team", "platform-team")
    else:
        # Generic infra actions whose params cannot be derived from evidence:
        # fall back to declared slot names with placeholders.
        for slot in declared:
            params.setdefault(slot, example_params.get(slot, "previous"))

    return params


def select_action(candidates: list[dict], actions_meta: dict, retrieval: dict,
                  incident_features: dict) -> dict:
    """Pick the final action and assemble the decision + reasoning."""
    root_service = incident_features.get("root_service")
    max_sim = retrieval.get("max_similarity", 0.0)
    ood = retrieval.get("ood", False)

    def page_decision(reason: str, confidence: float) -> dict:
        meta = actions_meta["page_oncall"]
        return {
            "selected_action": "page_oncall",
            "params": _build_params("page_oncall", meta, root_service, None),
            "confidence": round(confidence, 4),
            "utility": utility(confidence, meta),
            "selected_reason": reason,
        }

    # --- Gate 1: out-of-distribution -> escalate, do not guess. ----------
    if ood:
        return page_decision(
            f"OOD: max_similarity {max_sim:.3f} < threshold "
            f"{retrieval.get('ood_threshold')}. No historical precedent is close "
            f"enough to act on; escalating to human.",
            confidence=round(1.0 - max_sim, 4),
        )

    if not candidates:
        return page_decision("No candidate actions could be voted from neighbours.", 0.5)

    # Score every candidate by utility.
    enriched = []
    for c in candidates:
        meta = actions_meta.get(c["name"], {"cost_min": 0, "blast_radius_services": 0,
                                            "downtime_min": 0, "params": []})
        enriched.append({**c, "meta": meta, "utility": utility(c["confidence"], meta)})

    page_candidate = next((c for c in enriched if c["name"] == "page_oncall"), None)
    auto_candidates = [c for c in enriched if c["name"] != "page_oncall"]
    auto_candidates.sort(key=lambda c: c["utility"], reverse=True)
    best_auto = auto_candidates[0] if auto_candidates else None

    # Gate 2: neighbours voted to page and no auto-action beats it. ---
    # page_oncall wins on votes when it is the consensus precedent (e.g. TLS
    # cert rotation, cache-stampede) and there is no confidently-better fix.
    if page_candidate and (best_auto is None
                           or page_candidate["vote_score"] >= best_auto["vote_score"]):
        return page_decision(
            f"Outcome-weighted vote favours escalation "
            f"(page vote={page_candidate['vote_score']:.3f} >= "
            f"best auto vote={best_auto['vote_score']:.3f}). "
            f"Historical precedent for this signature was human-handled."
            if best_auto else
            f"Only escalation precedents matched (page vote="
            f"{page_candidate['vote_score']:.3f}).",
            confidence=page_candidate["confidence"],
        )

    # Gate 3: confidence floor.
    if best_auto["confidence"] < CONFIDENCE_FLOOR:
        return page_decision(
            f"Top auto-action '{best_auto['name']}' confidence "
            f"{best_auto['confidence']:.3f} < floor {CONFIDENCE_FLOOR}. "
            f"Too uncertain to auto-act; escalating.",
            confidence=round(1.0 - best_auto["confidence"], 4),
        )

    # Gate 4: risky blast radius needs high confidence.
    blast = best_auto["meta"].get("blast_radius_services", 0)
    if blast >= HIGH_BLAST and best_auto["confidence"] < HIGH_BLAST_CONF:
        return page_decision(
            f"Top auto-action '{best_auto['name']}' has blast radius {blast} "
            f"(>= {HIGH_BLAST}) but confidence {best_auto['confidence']:.3f} "
            f"< {HIGH_BLAST_CONF}. Refusing to auto-fire a high-impact action on "
            f"weak evidence; escalating.",
            confidence=round(1.0 - best_auto["confidence"], 4),
        )

    # Auto-act on the best candidate
    example = best_auto["supporters"][0]["example_params"] if best_auto["supporters"] else {}
    params = _build_params(best_auto["name"], best_auto["meta"], root_service, example)
    return {
        "selected_action": best_auto["name"],
        "params": params,
        "confidence": best_auto["confidence"],
        "utility": best_auto["utility"],
        "selected_reason": (
            f"Auto-act: '{best_auto['name']}' is the outcome-weighted vote winner "
            f"(vote={best_auto['vote_score']:.3f}, confidence="
            f"{best_auto['confidence']:.3f}, utility={best_auto['utility']:.3f}). "
            f"Blast radius {blast} within tolerance; targeted at root service "
            f"'{root_service}'."
        ),
    }
