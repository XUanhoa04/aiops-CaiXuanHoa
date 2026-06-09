"""
RCA Pipeline — Graph & Retrieval
"""

import json
from pathlib import Path
from collections import defaultdict
import networkx as nx

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_alerts(path):
    alerts = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                alerts.append(json.loads(line.strip()))
    return alerts

def build_graph(services_json_path):
    g = nx.DiGraph()
    data = load_json(services_json_path)
    for svc in data["services"]:
        g.add_node(svc["name"], **{k: v for k, v in svc.items() if k != "name"})
    for store in data["stores"]:
        g.add_node(store["name"], **{k: v for k, v in store.items() if k != "name"})
    for edge in data["edges"]:
        g.add_edge(edge["from"], edge["to"], type=edge["type"])
    return g

def graph_temporal_scorer(cluster, alerts, graph):
    """
    Score candidates based on PageRank and Temporal (earliest alert).
    Note: The original graph naturally points from Caller -> Callee.
    So Callee (Root Cause) gets the most incoming links -> Highest PageRank.
    """
    cluster_services = set(cluster["services"])
    cluster_alerts = [a for a in alerts if a["id"] in cluster["alert_ids"]]
    
    # Build subgraph
    subgraph = graph.subgraph(cluster_services).copy()
    if len(subgraph.nodes) == 0:
        return []
    
    # PageRank on original graph (so terminal nodes like payment-svc get highest score)
    try:
        pr_scores = nx.pagerank(subgraph, alpha=0.85)
    except:
        pr_scores = {node: 1.0/len(subgraph.nodes) for node in subgraph.nodes}
        
    max_pr = max(pr_scores.values()) if pr_scores else 1.0
    pr_norm = {k: v / max_pr if max_pr > 0 else 0 for k, v in pr_scores.items()}
    
    # Temporal score
    first_seen = {}
    for a in cluster_alerts:
        svc = a["service"]
        ts = a["ts"]
        if svc not in first_seen or ts < first_seen[svc]:
            first_seen[svc] = ts
            
    sorted_times = sorted(first_seen.values())
    temporal_score = {}
    if len(sorted_times) > 1:
        t_earliest = sorted_times[0]
        t_latest = sorted_times[-1]
        for svc, ts in first_seen.items():
            if t_latest == t_earliest:
                temporal_score[svc] = 1.0
            else:
                pass
    
    sorted_svcs_by_time = sorted(first_seen.keys(), key=lambda s: first_seen[s])
    n = len(sorted_svcs_by_time)
    for i, svc in enumerate(sorted_svcs_by_time):
        temporal_score[svc] = 1.0 - (i / max(1, n - 1))
        
    final_scores = []
    for svc in cluster_services:
        s_pr = pr_norm.get(svc, 0)
        s_time = temporal_score.get(svc, 0)
        score = 0.6 * s_pr + 0.4 * s_time
        final_scores.append((svc, score))
        
    final_scores.sort(key=lambda x: x[1], reverse=True)
    return final_scores

def retrieve_similar_incidents(cluster, incidents, top_k=3):
    cluster_services = set(cluster["services"])
    cluster_sev = cluster["max_severity"]
    
    scored_incidents = []
    for inc in incidents:
        score = 0.0
        # +0.4 if history.root_cause_service in cluster.services
        if inc["root_cause_service"] in cluster_services:
            score += 0.4
            
        # +0.2 for each overlap, max 0.4
        overlap = len(cluster_services.intersection(set(inc["services_involved"])))
        score += min(0.4, 0.2 * overlap)
        
        # +0.2 if same severity
        if inc["severity"] == cluster_sev:
            score += 0.2
            
        if score >= 0.2:
            scored_incidents.append((score, inc))
            
    scored_incidents.sort(key=lambda x: x[0], reverse=True)
    return scored_incidents[:top_k]

def rca_pipeline(cluster, alerts, graph, incidents):
    # 1. Graph + Temporal
    top_candidates = graph_temporal_scorer(cluster, alerts, graph)
    graph_top3 = [[s, round(score, 2)] for s, score in top_candidates[:3]]
    
    root_cause = graph_top3[0][0] if graph_top3 else "unknown"
    confidence = graph_top3[0][1] if graph_top3 else 0.0
    
    # 2. Retrieval (kNN-style)
    similar_incs = retrieve_similar_incidents(cluster, incidents, top_k=3)
    
    # 3. Classifier
    if similar_incs:
        best_match_score, best_inc = similar_incs[0]
        rc_class = best_inc["root_cause_class"]
        actions = [best_inc["remediation"]]
        sim_ids = [inc["id"] for _, inc in similar_incs]
        confidence = round((confidence + best_match_score) / 2, 2)
    else:
        rc_class = "other"
        actions = ["Investigate manually"]
        sim_ids = []
        
    result = {
        "cluster_id": cluster["cluster_id"],
        "graph_top3": graph_top3,
        "root_cause": root_cause,
        "class": rc_class,
        "confidence": confidence,
        "actions": actions,
        "reasoning": f"Graph traversal picked {root_cause} as most likely root cause. Retrieval matched {len(sim_ids)} past incidents.",
        "similar_incidents": sim_ids,
        "method": "graph+retrieval"
    }
    return result

def main():
    base_dir = Path(".")
    d1_results = Path("../d1/results/cluster_summary.json")
    if not d1_results.exists():
        d1_results = Path("d:/DevopsAndCloud/AIOPS/w2/d1/results/cluster_summary.json")
    
    clusters_data = load_json(d1_results)
    alerts = load_alerts(base_dir / "dataset/alerts_sample.jsonl")
    graph = build_graph(base_dir / "dataset/services.json")
    incidents_data = load_json(base_dir / "dataset/incidents_history.json")["incidents"]
    
    results = []
    for cluster in clusters_data["clusters"]:
        res = rca_pipeline(cluster, alerts, graph, incidents_data)
        results.append(res)
        
    output = {
        "clusters_analyzed": len(clusters_data["clusters"]),
        "results": results
    }
    
    out_dir = base_dir / "results"
    out_dir.mkdir(exist_ok=True)
    with open(out_dir / "rca_output.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
        
    print(json.dumps(output, indent=2))

if __name__ == "__main__":
    main()
