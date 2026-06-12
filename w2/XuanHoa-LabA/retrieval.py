"""Layer 2 - Similarity retrieval + outcome-weighted action voting.

The retrieval is a hybrid (weighted-fusion) score over four evidence channels:

    similarity = 0.35 * log + 0.30 * trace + 0.20 * service + 0.15 * metric

This mirrors hybrid retrieval in RAG: a lexical channel (logs), a structural
channel (trace topology), and two cheap metadata channels (service + metric
name overlap). Weights put logs first (the strongest class-of-incident signal)
and traces second (they break the "logs lie" ties), with service/metric overlap
as supporting evidence. See FINDINGS.md Q1 for the alternative (TF-IDF cosine)
and why Jaccard fusion won on a 29-entry corpus.

Voting is outcome-weighted: a neighbour's vote for an action is its similarity
times an outcome multiplier, so a *failed* action cannot win just because it sat
in the closest neighbour.
"""
from __future__ import annotations

from collections import defaultdict

# Channel weights for the fused similarity score.
# Logs carry the strongest class-of-incident signal, so they dominate; traces
# break "logs lie" ties; service/metric overlap are supporting evidence. Tuned
# on E01-E08: this split keeps genuine matches whose neighbour lacks trace data
# (E03 memory-leak) above the OOD line, while pushing pure-topology coincidences
# with no log agreement (E07 novel k8s incident) below it. See FINDINGS Q1.
W_LOG = 0.40
W_TRACE = 0.25
W_SERVICE = 0.15
W_METRIC = 0.20

# Outcome multipliers applied on top of similarity when voting.
OUTCOME_WEIGHT = {"success": 1.0, "partial": 0.5, "failed": -0.3}

# Below this fused similarity the closest neighbour is not trustworthy -> OOD.
OOD_THRESHOLD = 0.30


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def service_overlap_score(live: dict, hist: dict) -> float:
    """Jaccard of affected-service sets."""
    return _jaccard(set(live.get("affected_services", [])),
                    set(hist.get("affected_services", [])))


def log_similarity_score(live: dict, hist: dict) -> float:
    """Token-set Jaccard between (trusted) live log templates and historical
    log signatures. Robust on a tiny corpus where TF-IDF idf weights are noisy."""
    return _jaccard(set(live.get("log_token_set", [])),
                    set(hist.get("log_token_set", [])))


def trace_similarity_score(live: dict, hist: dict) -> float:
    """Blend of directed-edge overlap and anomalous-edge overlap.

    The anomalous-edge term is what lets E06 match the cart-svc -> cart-redis
    network-partition precedent rather than a payment-svc pool precedent.
    """
    live_edges = {(a, b) for a, b in live.get("trace_edges", [])}
    hist_edges = {(e[0], e[1]) for e in hist.get("trace_edges", [])}
    edge_j = _jaccard(live_edges, hist_edges)

    live_err = {(e["from"], e["to"]) for e in live.get("trace_error_edges", [])}
    hist_err = {(e["from"], e["to"]) for e in hist.get("trace_error_edges", [])}
    # Also credit a reversed-direction match (corpus sometimes records the edge
    # from the DB side, e.g. payments-db -> payment-svc).
    hist_err_rev = {(b, a) for a, b in hist_err}
    err_j = max(_jaccard(live_err, hist_err), _jaccard(live_err, hist_err_rev))

    if not live_err and not hist_err:
        return edge_j
    return 0.5 * edge_j + 0.5 * err_j


def metric_similarity_score(live: dict, hist: dict) -> float:
    """Jaccard over metric base-names (e.g. latency_p99_ms, conn_pool_used)."""
    return _jaccard(set(live.get("metric_names", [])),
                    set(hist.get("metric_names", [])))


def similarity(live: dict, hist: dict) -> dict:
    """Full fused similarity with a per-channel breakdown for the audit trail."""
    log = log_similarity_score(live, hist)
    trace = trace_similarity_score(live, hist)
    service = service_overlap_score(live, hist)
    metric = metric_similarity_score(live, hist)
    total = W_LOG * log + W_TRACE * trace + W_SERVICE * service + W_METRIC * metric
    return {
        "similarity": round(total, 4),
        "log": round(log, 4),
        "trace": round(trace, 4),
        "service": round(service, 4),
        "metric": round(metric, 4),
    }


def retrieve_similar(live_features: dict, history_features: list[dict],
                     top_k: int = 5, ood_threshold: float = OOD_THRESHOLD) -> dict:
    """Rank historical incidents by fused similarity and flag OOD inputs."""
    scored = []
    for hist in history_features:
        s = similarity(live_features, hist)
        scored.append({
            "id": hist["id"],
            "root_cause_class": hist["root_cause_class"],
            "similarity": s["similarity"],
            "breakdown": s,
            "outcome": hist["outcome"],
            "actions": hist["actions"],
            "affected_services": hist["affected_services"],
        })
    scored.sort(key=lambda x: x["similarity"], reverse=True)
    neighbors = scored[:top_k]
    max_sim = neighbors[0]["similarity"] if neighbors else 0.0
    ood = max_sim < ood_threshold
    return {
        "neighbors": neighbors,
        "max_similarity": max_sim,
        "ood": ood,
        "ood_threshold": ood_threshold,
    }


def rank_candidate_actions(neighbors: list[dict]) -> list[dict]:
    """Outcome-weighted vote over the actions taken by the retrieved neighbours.

    Each neighbour casts one vote per distinct action *name* it took, weighted by
    ``similarity * outcome_weight``. Votes are grouped by action name (the
    service parameter is re-targeted to the live incident's root in Layer 3, so
    we vote on the *kind* of action, not on a stale service name).

    Confidence is the winning action's share of the total positive vote mass.
    """
    raw: dict[str, float] = defaultdict(float)
    supporters: dict[str, list] = defaultdict(list)

    for nb in neighbors:
        seen_names = set()
        for action in nb["actions"]:
            name = action["name"]
            if name in seen_names:
                continue            # one vote per neighbour per action name
            seen_names.add(name)
            weight = nb["similarity"] * OUTCOME_WEIGHT.get(nb["outcome"], 0.0)
            raw[name] += weight
            supporters[name].append({
                "id": nb["id"],
                "similarity": nb["similarity"],
                "outcome": nb["outcome"],
                "contribution": round(weight, 4),
                "example_params": action.get("params", {}),
            })

    positive_mass = sum(v for v in raw.values() if v > 0) or 1e-9
    candidates = []
    for name, score in raw.items():
        candidates.append({
            "name": name,
            "vote_score": round(score, 4),
            "confidence": round(max(0.0, score) / positive_mass, 4),
            "supporters": sorted(supporters[name],
                                 key=lambda s: s["contribution"], reverse=True),
        })
    candidates.sort(key=lambda c: c["vote_score"], reverse=True)
    return candidates
