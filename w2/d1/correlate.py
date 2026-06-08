import json
import networkx as nx
from collections import defaultdict
from datetime import datetime, timedelta, timezone


# Layer 0 — Data Loading

def load_alerts(path: str) -> list[dict]:
    """Load alerts from a JSONL file (one JSON object per line)."""
    alerts = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                alerts.append(json.loads(line))
    return alerts


def build_graph(services_json_path: str) -> nx.DiGraph:
    """
    Build directed service graph from services.json.
    A → B means A calls/depends on B.
    """
    g = nx.DiGraph()
    with open(services_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    for svc in data["services"]:
        g.add_node(svc["name"], **{k: v for k, v in svc.items() if k != "name"})

    for store in data["stores"]:
        g.add_node(store["name"], **{k: v for k, v in store.items() if k != "name"})

    for edge in data["edges"]:
        g.add_edge(edge["from"], edge["to"], type=edge["type"])

    return g



# Layer 1 — Fingerprint & Dedup

def fingerprint(alert: dict) -> str:
    """
    Create a unique dedup key for an alert.
    Fields: service + metric + severity.
    
    NOT included: timestamp, value (they change every fire → dedup would be useless).
    """
    return f"{alert['service']}|{alert['metric']}|{alert['severity']}"


class Deduper:
    """Stateful dedup store: fingerprint → cluster info."""

    def __init__(self):
        self.store: dict[str, dict] = {}

    def push(self, alert: dict) -> str:
        fp = fingerprint(alert)
        ts = datetime.fromisoformat(alert["ts"].replace("Z", "+00:00"))

        if fp not in self.store:
            self.store[fp] = {
                "fingerprint": fp,
                "count": 1,
                "first_seen": ts,
                "last_seen": ts,
                "alerts": [alert["id"]],
                "max_severity": alert["severity"],
                "service": alert["service"],
            }
        else:
            c = self.store[fp]
            c["count"] += 1
            c["last_seen"] = ts
            c["alerts"].append(alert["id"])
        return fp

    def clusters(self) -> list[dict]:
        return list(self.store.values())


# Layer 2 — Session Window (time-based grouping)

def session_groups(alerts: list[dict], gap_sec: int = 120) -> list[list[dict]]:
    """
    Group alerts into sessions. A session ends when no alert arrives
    within gap_sec seconds of the previous alert in that session.
    
    Args:
        alerts: list of alert dicts (will be sorted by timestamp)
        gap_sec: max gap between consecutive alerts in same session
    
    Returns:
        list of groups, each group is a list of alert dicts
    """
    if not alerts:
        return []

    sorted_alerts = sorted(alerts, key=lambda a: a["ts"])
    groups = [[sorted_alerts[0]]]

    for alert in sorted_alerts[1:]:
        ts = datetime.fromisoformat(alert["ts"].replace("Z", "+00:00"))
        last_ts = datetime.fromisoformat(groups[-1][-1]["ts"].replace("Z", "+00:00"))

        if (ts - last_ts).total_seconds() <= gap_sec:
            groups[-1].append(alert)
        else:
            groups.append([alert])

    return groups



# Layer 3 — Topology-Aware Grouping

def topology_group(alerts: list[dict], graph: nx.DiGraph, max_hop: int = 2) -> list[list[dict]]:
    """
    Group alerts whose services are within max_hop distance on the
    undirected service graph. Uses Union-Find for efficient grouping.
    
    Args:
        alerts: list of alert dicts (from same time-session)
        graph: directed service graph
        max_hop: max graph distance to consider services "related"
    
    Returns:
        list of groups, each group is a list of alert dicts
    """
    if not alerts:
        return []

    undirected = graph.to_undirected()

    # service → alerts at that service
    by_service: dict[str, list[dict]] = defaultdict(list)
    for a in alerts:
        by_service[a["service"]].append(a)

    services_with_alerts = list(by_service.keys())

    # If only 1 service, return single group
    if len(services_with_alerts) <= 1:
        return [alerts]

    # Union-Find with path compression
    parent = {s: s for s in services_with_alerts}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    # Union services within max_hop distance
    for i, s1 in enumerate(services_with_alerts):
        for s2 in services_with_alerts[i + 1:]:
            try:
                dist = nx.shortest_path_length(undirected, s1, s2)
                if dist <= max_hop:
                    union(s1, s2)
            except nx.NetworkXNoPath:
                continue

    # Collect groups
    groups_dict: dict[str, list[dict]] = defaultdict(list)
    for s in services_with_alerts:
        groups_dict[find(s)].extend(by_service[s])

    return list(groups_dict.values())


# Combined Pipeline


# Severity ordering for max_severity calculation
_SEV_ORDER = {"info": 0, "warn": 1, "crit": 2, "fatal": 3}


def correlate(
    alerts: list[dict],
    graph: nx.DiGraph,
    gap_sec: int = 120,
    max_hop: int = 2,
) -> list[dict]:
    """
    Full correlation pipeline:
      1. Sort alerts by timestamp
      2. Session-window grouping (gap_sec)
      3. Within each session, topology grouping (max_hop)
      4. Produce cluster summaries with fingerprints
    
    Returns:
        list of cluster dicts with cluster_id, alert_count, services,
        alert_ids, fingerprints, time_range, max_severity
    """
    sessions = session_groups(alerts, gap_sec=gap_sec)

    all_clusters = []
    for session_idx, session_alerts in enumerate(sessions):
        topo_groups = topology_group(session_alerts, graph, max_hop=max_hop)
        for group_idx, group in enumerate(topo_groups):
            # Compute fingerprints for this cluster
            fps = sorted(set(fingerprint(a) for a in group))

            # Max severity by ordering
            max_sev = max(group, key=lambda a: _SEV_ORDER.get(a["severity"], 0))["severity"]

            all_clusters.append({
                "cluster_id": f"c-{session_idx:03d}-{group_idx:03d}",
                "alert_count": len(group),
                "services": sorted(set(a["service"] for a in group)),
                "alert_ids": [a["id"] for a in group],
                "fingerprints": fps,
                "time_range": [
                    min(a["ts"] for a in group),
                    max(a["ts"] for a in group),
                ],
                "max_severity": max_sev,
            })

    return all_clusters


def build_summary(alerts: list[dict], clusters: list[dict]) -> dict:
    """Build the final cluster_summary.json output."""
    n_in = len(alerts)
    n_out = len(clusters)
    return {
        "input_alerts": n_in,
        "output_clusters": n_out,
        "reduction_ratio": round(1 - n_out / n_in, 4) if n_in > 0 else 0,
        "clusters": clusters,
    }
