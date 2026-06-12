
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict


# Log normalisation
# Domain keywords we always preserve verbatim because they carry class-of-incident
# signal. Everything dynamic (numbers, ids, hosts, hex, uuids) is stripped.
KEYWORDS = {
    "connectionpool", "connection", "pool", "exhausted", "timeout", "acquiring",
    "dns", "nxdomain", "resolution", "tls", "handshake", "certificate", "expired",
    "x509", "network", "policy", "redis", "kafka", "payment", "checkout",
    "deadlock", "lock", "wait", "forward", "request", "upstream", "downstream",
    "retry", "exhausted", "fallback", "oom", "outofmemoryerror", "heap", "memory",
    "gc", "pause", "evicted", "cgroup", "kill", "query", "latency", "db",
    "rebalance", "partition", "reassignment", "consumer", "lag", "ratelimit",
    "rate", "limit", "throttled", "informer", "cache", "sync", "kubernetes",
    "api", "replica", "drift", "feature", "model", "inference", "degraded",
    "error", "elevated", "503", "5xx", "429", "backend", "refused", "stale",
}

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_HEX_RE = re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I)
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}t[\d:\.]+z?", re.I)
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_KV_ID_RE = re.compile(r"\b(id|attempt|retries|host|target|after_ms|notafter|cn|waited)\s*=\s*\S+", re.I)
_TOKEN_RE = re.compile(r"[a-zA-Z_]+")


def normalize_log(msg: str) -> str:
    """Turn a raw log line into a stable template string.

    Lowercases, drops timestamps / numbers / ids / hex / uuids, and keeps the
    meaningful alphabetic keywords. The same routine is applied to historical
    ``log_signatures`` so the two vocabularies line up.

    >>> normalize_log("ConnectionPool: timeout acquiring connection (waited 5000ms) attempt=7")
    'connectionpool timeout acquiring connection waited ms'
    """
    if not msg:
        return ""
    s = msg.lower()
    s = _UUID_RE.sub(" ", s)
    s = _TS_RE.sub(" ", s)
    s = _KV_ID_RE.sub(" ", s)
    s = _HEX_RE.sub(" ", s)
    s = _NUM_RE.sub(" ", s)
    tokens = _TOKEN_RE.findall(s)
    # Drop 1-char noise tokens but keep everything else (semantic content).
    tokens = [t for t in tokens if len(t) > 1]
    return " ".join(tokens)


def log_tokens(template: str) -> set[str]:
    """Token set of a normalised template (used for Jaccard similarity)."""
    return set(template.split())


def keyword_hits(template: str) -> list[str]:
    """Which domain keywords appear in this template."""
    toks = set(template.split())
    return sorted(toks & KEYWORDS)

# Action-string parsing  (historical "name:p1:p2" -> actions.yaml schema)
# Positional parameter names per action, mirroring actions.yaml.
_ACTION_PARAM_NAMES = {
    "rollback_service": ["service", "target_version"],
    "increase_pool_size": ["service", "from_value", "to_value"],
    "restart_pod": ["service", "pod_selector"],
    "dns_config_rollback": ["configmap_name", "target_revision"],
    "network_policy_revert": ["policy_name"],
    "page_oncall": ["team"],
}


def parse_action_string(action_str: str, actions_catalog: list[dict] | None = None) -> dict:
    """Parse a historical action string into ``{"name", "params"}``.

    The positional params are mapped onto the named params declared in
    ``actions.yaml`` (passed in as ``actions_catalog``) when available, otherwise
    onto the built-in defaults above.

    >>> parse_action_string("rollback_service:payment-svc:v3.1")
    {'name': 'rollback_service', 'params': {'service': 'payment-svc', 'target_version': 'v3.1'}}
    >>> parse_action_string("increase_pool_size:payment-svc:50:100")
    {'name': 'increase_pool_size', 'params': {'service': 'payment-svc', 'from_value': '50', 'to_value': '100'}}
    """
    parts = action_str.split(":")
    name = parts[0]
    values = parts[1:]

    names = _ACTION_PARAM_NAMES.get(name)
    if names is None and actions_catalog:
        for a in actions_catalog:
            if a.get("name") == name:
                names = a.get("params", [])
                break
    if names is None:
        names = [f"arg{i}" for i in range(len(values))]

    params: dict[str, str] = {}
    for i, val in enumerate(values):
        key = names[i] if i < len(names) else f"arg{i}"
        params[key] = val
    return {"name": name, "params": params}

# Live-incident sub-extractors

ANOMALY_ERROR_RATE = 0.05      # an edge above this error-rate is anomalous
METRIC_SPIKE_RATIO = 1.5       # tail/baseline ratio above this is a spike


def extract_log_features(live_incident: dict) -> dict:
    """Cluster the raw log lines into stable templates.

    Returns log_templates (unique normalised ERROR templates), keyword counts,
    the per-template service map, and the set of services that emitted ERROR
    logs. INFO lines are ignored -- they are background noise.
    """
    logs = live_incident.get("logs", [])
    template_counts: Counter[str] = Counter()
    keyword_counts: Counter[str] = Counter()
    log_services: Counter[str] = Counter()
    template_services: dict[str, Counter] = defaultdict(Counter)

    for entry in logs:
        if entry.get("level") != "ERROR":
            continue
        svc = entry.get("svc", "")
        tmpl = normalize_log(entry.get("msg", ""))
        if not tmpl:
            continue
        template_counts[tmpl] += 1
        log_services[svc] += 1
        template_services[tmpl][svc] += 1
        for kw in keyword_hits(tmpl):
            keyword_counts[kw] += 1

    return {
        "log_templates": list(template_counts.keys()),
        "log_template_counts": dict(template_counts),
        "log_keyword_counts": dict(keyword_counts),
        "log_services": dict(log_services),
        "template_services": {k: dict(v) for k, v in template_services.items()},
    }


def extract_trace_features(live_incident: dict) -> dict:
    """Aggregate raw trace records per directed edge.

    Computes error_rate = error_count / count and the max p99 per edge, then
    flags high-error edges and slow edges. The callee (``to``) of an anomalous
    edge is the prime suspect; the caller (``from``) is a secondary suspect.
    """
    agg: dict[tuple, dict] = {}
    for t in live_incident.get("traces", []):
        key = (t.get("from"), t.get("to"))
        a = agg.setdefault(key, {"count": 0, "error_count": 0, "p99_ms": 0.0})
        a["count"] += t.get("count", 0)
        a["error_count"] += t.get("error_count", 0)
        a["p99_ms"] = max(a["p99_ms"], t.get("p99_ms", 0.0))

    edges = []
    high_error_edges = []
    slow_edges = []
    p99s = [v["p99_ms"] for v in agg.values()] or [0.0]
    p99_median = statistics.median(p99s)
    for (frm, to), v in agg.items():
        er = v["error_count"] / v["count"] if v["count"] else 0.0
        rec = {
            "from": frm, "to": to, "count": v["count"],
            "error_count": v["error_count"], "error_rate": round(er, 4),
            "p99_ms": round(v["p99_ms"], 1),
        }
        edges.append(rec)
        if er >= ANOMALY_ERROR_RATE:
            high_error_edges.append(rec)
        if v["p99_ms"] > max(300.0, p99_median * 2.0):
            slow_edges.append(rec)

    affected = set()
    for e in high_error_edges:
        affected.add(e["from"])
        affected.add(e["to"])

    return {
        "trace_edges": [(e["from"], e["to"]) for e in edges],
        "trace_edge_detail": edges,
        "trace_error_edges": high_error_edges,
        "trace_slow_edges": slow_edges,
        "trace_affected_services": sorted(affected),
    }


def _series_ratio(series: list) -> tuple[float, float, float]:
    """(baseline_mean, tail_mean, max) over a metric series."""
    vals = [v for _, v in series if isinstance(v, (int, float))]
    if not vals:
        return 0.0, 0.0, 0.0
    n = len(vals)
    third = max(1, n // 3)
    base = statistics.mean(vals[:third])
    tail = statistics.mean(vals[-third:])
    return base, tail, max(vals)


def extract_metric_features(live_incident: dict) -> dict:
    """Per-series spike detection. Metric name parsed as ``service.metric``."""
    samples = live_incident.get("metrics_window", {}).get("samples", {})
    signals = []
    suspicious = []
    for name, series in samples.items():
        svc, _, metric = name.partition(".")
        base, tail, mx = _series_ratio(series)
        if base and abs(base) > 1e-9:
            ratio = tail / base
        else:
            ratio = float("inf") if tail > 1.0 else 1.0
        sig = {
            "name": name, "service": svc, "metric": metric,
            "delta_ratio": round(ratio, 3) if ratio != float("inf") else 9999.0,
            "max": round(mx, 3),
        }
        signals.append(sig)
        if ratio == float("inf") or ratio > METRIC_SPIKE_RATIO:
            suspicious.append(sig)
    return {
        "metric_signals": signals,
        "metric_suspicious": suspicious,
        "metric_spike_services": sorted({s["service"] for s in suspicious}),
        "metric_names": sorted({s["metric"] for s in signals}),
    }


def derive_affected_services(live_incident: dict, log_f: dict, trace_f: dict,
                             metric_f: dict) -> dict:
    """Rank candidate affected services by multi-signal corroboration.

    Scoring weights (the heart of the conflicting-evidence handling):
        trace callee (downstream culprit)  +3.0
        trace caller (upstream involved)   +2.0
        metric spike on the service        +2.0
        trigger-alert service              +2.0
        ERROR-log burst from the service   +1.0  (only counts toward ranking,
                                                  never qualifies a service alone)

    A service is *affected* (kept) only if corroborated by trace, metric, or the
    trigger. A service seen ONLY in logs is recorded but excluded from the
    affected set -- defeating the E06 "logs lie" trap.
    """
    trigger = live_incident.get("trigger_alert", {}).get("service")
    scores: dict[str, float] = defaultdict(float)
    reasons: dict[str, list] = defaultdict(list)
    corroborated: set[str] = set()

    if trigger:
        scores[trigger] += 2.0
        reasons[trigger].append("trigger_alert")
        corroborated.add(trigger)

    for e in trace_f["trace_error_edges"]:
        scores[e["to"]] += 3.0
        reasons[e["to"]].append(f"trace_callee:{e['from']}->{e['to']}(er={e['error_rate']})")
        corroborated.add(e["to"])
        scores[e["from"]] += 2.0
        reasons[e["from"]].append(f"trace_caller:{e['from']}->{e['to']}(er={e['error_rate']})")
        corroborated.add(e["from"])

    for s in metric_f["metric_suspicious"]:
        scores[s["service"]] += 2.0
        reasons[s["service"]].append(f"metric_spike:{s['metric']}(x{s['delta_ratio']})")
        corroborated.add(s["service"])

    # Log burst adds weight but never qualifies a service on its own.
    for svc, cnt in log_f["log_services"].items():
        scores[svc] += 1.0
        reasons[svc].append(f"log_burst:{cnt}")

    affected = sorted(
        (s for s in scores if s in corroborated),
        key=lambda s: scores[s], reverse=True,
    )
    log_only = sorted(s for s in scores if s not in corroborated)
    root = affected[0] if affected else (trigger or (log_only[0] if log_only else None))

    return {
        "affected_services": affected,
        "root_service": root,
        "service_scores": {s: round(scores[s], 2) for s in scores},
        "service_reasons": {s: reasons[s] for s in scores},
        "log_only_services": log_only,
    }


def _feature_text(log_templates, trace_edges, affected, metric_names) -> str:
    parts = []
    parts.extend(log_templates)
    parts.extend(f"{a}->{b}" for a, b in trace_edges)
    parts.extend(affected)
    parts.extend(metric_names)
    return " ".join(parts)


def build_incident_features(live_incident: dict) -> dict:
    """Assemble the comparable feature dict for a live incident."""
    log_f = extract_log_features(live_incident)
    trace_f = extract_trace_features(live_incident)
    metric_f = extract_metric_features(live_incident)
    affected_f = derive_affected_services(live_incident, log_f, trace_f, metric_f)

    affected = affected_f["affected_services"]
    affected_set = set(affected)

    # Logs are only trusted for similarity when emitted by a corroborated
    # (affected) service. This keeps "lying" log floods out of the comparison.
    trusted_templates = [
        t for t, svcs in log_f["template_services"].items()
        if affected_set & set(svcs.keys())
    ]
    if not trusted_templates:
        trusted_templates = log_f["log_templates"]

    feature_text = _feature_text(
        trusted_templates, trace_f["trace_edges"], affected, metric_f["metric_names"]
    )

    return {
        "incident_id": live_incident.get("incident_id"),
        "trigger_service": live_incident.get("trigger_alert", {}).get("service"),
        "severity": live_incident.get("trigger_alert", {}).get("severity"),
        "rule_id": live_incident.get("trigger_alert", {}).get("rule_id"),
        "affected_services": affected,
        "root_service": affected_f["root_service"],
        "service_scores": affected_f["service_scores"],
        "service_reasons": affected_f["service_reasons"],
        "log_only_services": affected_f["log_only_services"],
        "log_templates": trusted_templates,
        "all_log_templates": log_f["log_templates"],
        "log_keyword_counts": log_f["log_keyword_counts"],
        "log_services": log_f["log_services"],
        "trace_edges": trace_f["trace_edges"],
        "trace_error_edges": trace_f["trace_error_edges"],
        "trace_slow_edges": trace_f["trace_slow_edges"],
        "metric_signals": metric_f["metric_suspicious"],
        "metric_names": metric_f["metric_names"],
        "metric_spike_services": metric_f["metric_spike_services"],
        "feature_text": feature_text,
        "log_token_set": sorted(set(" ".join(trusted_templates).split())),
    }


def build_history_features(history_entry: dict, actions_catalog: list[dict] | None = None) -> dict:
    """Assemble the comparable feature dict for one historical corpus entry.

    * log_signatures -> normalised log templates
    * trace_signatures -> trace edges + anomalous edge set
    * metric_signatures -> numeric delta ratios
    * actions_taken -> parsed via parse_action_string
    """
    templates = [normalize_log(s) for s in history_entry.get("log_signatures", [])]
    templates = [t for t in templates if t]

    trace_edges = []
    trace_error_edges = []
    for ts in history_entry.get("trace_signatures", []):
        edge = (ts.get("from"), ts.get("to"))
        trace_edges.append(edge)
        rec = {
            "from": ts.get("from"), "to": ts.get("to"),
            "error_rate": ts.get("error_rate", 0.0),
            "p99_deviation_ratio": ts.get("p99_deviation_ratio", 0.0),
        }
        if ts.get("error_rate", 0.0) >= ANOMALY_ERROR_RATE:
            trace_error_edges.append(rec)

    metric_signals = []
    metric_names = set()
    for ms in history_entry.get("metric_signatures", []):
        before, after = _parse_delta(ms.get("delta", ""))
        ratio = (after / before) if before else (9999.0 if after else 1.0)
        metric_signals.append({
            "service": ms.get("service"), "metric": ms.get("metric"),
            "delta_ratio": round(ratio, 3),
        })
        if ms.get("metric"):
            metric_names.add(ms.get("metric"))

    actions = [parse_action_string(a, actions_catalog) for a in history_entry.get("actions_taken", [])]
    affected = history_entry.get("affected_services", [])

    feature_text = _feature_text(templates, trace_edges, affected, sorted(metric_names))

    return {
        "id": history_entry.get("id"),
        "root_cause_class": history_entry.get("root_cause_class"),
        "affected_services": affected,
        "log_templates": templates,
        "trace_edges": trace_edges,
        "trace_error_edges": trace_error_edges,
        "metric_signals": metric_signals,
        "metric_names": sorted(metric_names),
        "actions": actions,
        "outcome": history_entry.get("outcome", "success"),
        "mttr_minutes": history_entry.get("mttr_minutes"),
        "feature_text": feature_text,
        "log_token_set": sorted(set(" ".join(templates).split())),
    }


def _parse_delta(s: str) -> tuple[float, float]:
    """Parse a ``"30 -> 99"`` metric-delta string into (before, after)."""
    if not s:
        return 0.0, 0.0
    parts = s.replace("->", "|").split("|")
    if len(parts) != 2:
        return 0.0, 0.0
    try:
        return float(parts[0].strip()), float(parts[1].strip())
    except ValueError:
        return 0.0, 0.0
