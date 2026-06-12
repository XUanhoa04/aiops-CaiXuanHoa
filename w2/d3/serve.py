"""
serve.py — AIOps W2-D3 Model Serving (HOA)

Đưa pipeline correlation (W2-D1) + RCA (W2-D2) lên thành 1 HTTP service.

Flow trong endpoint POST /incident:
    raw alerts (JSON)
      -> validate (Pydantic)
      -> correlate(alerts, GRAPH, gap_sec=120, max_hop=2)   [Layer 1, từ w2/d1]
      -> pick cluster lớn nhất làm primary incident
      -> rca_pipeline(primary, alerts, GRAPH, HISTORY)       [Layer 2, từ w2/d2]
      -> (optional) LLM enrichment, gated bằng env AIOPS_USE_LLM
      -> serialize -> IncidentResponse (JSON)

Chạy:
    uvicorn serve:app --port 8000 --workers 1
    (máy yếu OK — single process, RAM idle ~150MB)

Feature flags (env):
    AIOPS_USE_LLM   = "true" | "false"   (default "false" — bypass LLM, graph+retrieval only)
    AIOPS_GAP_SEC   = int  (default 120)
    AIOPS_MAX_HOP   = int  (default 2)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# 0. Wiring — import real correlate (d1) + real rca_pipeline (d2)
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve().parent          # .../w2/d3
W2_DIR = HERE.parent                             # .../w2
D1_DIR = W2_DIR / "d1"
D2_DIR = W2_DIR / "d2"

# Đặt d1 & d2 lên sys.path để import module thật (không copy-paste logic).
for p in (D1_DIR, D2_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from correlate import correlate, build_graph          # noqa: E402  (w2/d1/correlate.py)
from rca import rca_pipeline as run_rca                # noqa: E402  (w2/d2/rca.py)

# Dataset thật dùng cho graph + history (source-of-truth từ d2).
SERVICES_JSON = D2_DIR / "dataset" / "services.json"
HISTORY_JSON = D2_DIR / "dataset" / "incidents_history.json"

# --------------------------------------------------------------------------- #
# 1. Config + feature flags
# --------------------------------------------------------------------------- #
APP_VERSION = "1.0.0"


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


USE_LLM = _env_bool("AIOPS_USE_LLM", False)
GAP_SEC = int(os.getenv("AIOPS_GAP_SEC", "120"))
MAX_HOP = int(os.getenv("AIOPS_MAX_HOP", "2"))

PIPELINE_CONFIG = {
    "gap_sec": GAP_SEC,
    "max_hop": MAX_HOP,
    "rca_method": "graph+retrieval",
    "llm_enabled": USE_LLM,
    "llm_model": os.getenv("AIOPS_LLM_MODEL", "gpt-4o-mini"),
}

# --------------------------------------------------------------------------- #
# 2. Structured JSON logging
# --------------------------------------------------------------------------- #
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


logger = logging.getLogger("aiops.serve")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(JsonFormatter())
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# --------------------------------------------------------------------------- #
# 3. Module-level state — load 1 lần khi import (không reload mỗi request)
# --------------------------------------------------------------------------- #
GRAPH = build_graph(str(SERVICES_JSON))

with open(HISTORY_JSON, "r", encoding="utf-8") as f:
    HISTORY: List[dict] = json.load(f)["incidents"]

GRAPH_LOADED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

logger.info(
    "state loaded",
    extra={"extra": {
        "graph_nodes": GRAPH.number_of_nodes(),
        "graph_edges": GRAPH.number_of_edges(),
        "history_count": len(HISTORY),
        "use_llm": USE_LLM,
    }},
)

# --------------------------------------------------------------------------- #
# 4. Optional Prometheus metrics (graceful nếu chưa cài)
# --------------------------------------------------------------------------- #
try:
    from prometheus_client import Counter, Histogram, make_asgi_app

    REQS = Counter("aiops_incident_requests_total", "Incident requests", ["status"])
    LAT = Histogram("aiops_incident_latency_seconds", "Incident latency seconds")
    CLUSTERS = Histogram("aiops_clusters_per_request", "Clusters per request")
    LLM_FAIL = Counter("aiops_llm_failures_total", "LLM failures", ["reason"])
    _PROM = True
except Exception:  # pragma: no cover
    _PROM = False

# --------------------------------------------------------------------------- #
# 5. Optional LLM cache (graceful nếu chưa cài cachetools)
# --------------------------------------------------------------------------- #
try:
    from cachetools import TTLCache

    _LLM_CACHE: Optional[TTLCache] = TTLCache(maxsize=1000, ttl=3600)
except Exception:  # pragma: no cover
    _LLM_CACHE = None


# --------------------------------------------------------------------------- #
# 6. Pydantic schemas
# --------------------------------------------------------------------------- #
class Alert(BaseModel):
    id: str
    ts: str
    service: str
    metric: str
    severity: str
    value: float
    threshold: float
    labels: Dict[str, Any] = Field(default_factory=dict)


class IncidentRequest(BaseModel):
    alerts: List[Alert]


class RootCause(BaseModel):
    service: str
    cls: str = Field(alias="class")
    confidence: float
    reasoning: str

    model_config = {"populate_by_name": True}


class IncidentResponse(BaseModel):
    clusters: List[dict]
    root_cause: RootCause
    recommended_actions: List[str]
    similar_incidents: List[str]
    method: str
    meta: dict


# --------------------------------------------------------------------------- #
# 7. Optional LLM enrichment (gated bằng feature flag, luôn có fallback)
# --------------------------------------------------------------------------- #
def _llm_enrich(graph_result: dict, primary: dict) -> dict:
    """
    Gọi LLM để refine class/actions. Có timeout + cache + hallucination guard.
    Nếu fail/invalid -> raise để caller fallback về graph result.
    Đây là IO-bound call thật; khi không có API key sẽ raise và fallback.
    """
    prompt = json.dumps({
        "cluster_services": primary["services"],
        "graph_top3": graph_result["graph_top3"],
        "similar": graph_result["similar_incidents"],
    }, sort_keys=True)
    key = hashlib.sha256(prompt.encode()).hexdigest()

    if _LLM_CACHE is not None and key in _LLM_CACHE:
        return _LLM_CACHE[key]

    from openai import OpenAI  # raise ImportError nếu chưa cài -> fallback

    client = OpenAI(timeout=10.0, max_retries=2)
    resp = client.chat.completions.create(
        model=PIPELINE_CONFIG["llm_model"],
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You are a senior SRE. Respond only valid JSON "
             "with keys: root_cause, class, confidence, actions, reasoning."},
            {"role": "user", "content": prompt},
        ],
    )
    out = json.loads(resp.choices[0].message.content)

    # Hallucination guard — root_cause phải nằm trong cluster services.
    if out.get("root_cause") not in primary["services"]:
        raise ValueError("llm root_cause not in cluster")
    if not isinstance(out.get("actions"), list) or not out["actions"]:
        raise ValueError("llm actions empty")

    enriched = {
        "root_cause": out["root_cause"],
        "class": out.get("class", graph_result["class"]),
        "confidence": float(out.get("confidence", graph_result["confidence"])),
        "actions": out["actions"],
        "reasoning": out.get("reasoning", graph_result["reasoning"]),
        "similar_incidents": graph_result["similar_incidents"],
        "graph_top3": graph_result["graph_top3"],
        "method": "graph+llm",
    }
    if _LLM_CACHE is not None:
        _LLM_CACHE[key] = enriched
    return enriched


# --------------------------------------------------------------------------- #
# 8. Glue pipeline — end-to-end thật
# --------------------------------------------------------------------------- #
def process_batch(alerts: List[dict]) -> dict:
    """Run real correlate -> pick primary -> real RCA -> (opt) LLM. Trả về dict + per-phase timings."""
    timings: Dict[str, float] = {}

    # --- correlate (Layer 1, d1) ---
    t0 = time.perf_counter()
    clusters = correlate(alerts, GRAPH, gap_sec=GAP_SEC, max_hop=MAX_HOP)
    timings["correlate_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    if not clusters:
        return {
            "clusters": [],
            "root_cause": {"service": "unknown", "class": "other",
                           "confidence": 0.0, "reasoning": "no clusters formed"},
            "recommended_actions": ["Investigate manually"],
            "similar_incidents": [],
            "method": "none",
            "timings": timings,
        }

    # primary = cluster lớn nhất theo alert_count
    primary = max(clusters, key=lambda c: c["alert_count"])

    # --- RCA (Layer 2, d2: graph traversal + temporal + retrieval) ---
    t1 = time.perf_counter()
    rca = run_rca(primary, alerts, GRAPH, HISTORY)
    timings["rca_ms"] = round((time.perf_counter() - t1) * 1000, 2)

    method = rca["method"]
    actions = rca["actions"]
    rc_class = rca["class"]
    confidence = rca["confidence"]
    reasoning = rca["reasoning"]
    root_cause_svc = rca["root_cause"]
    similar = rca["similar_incidents"]

    # --- optional LLM enrichment (gated) ---
    t2 = time.perf_counter()
    if USE_LLM:
        try:
            enriched = _llm_enrich(rca, primary)
            root_cause_svc = enriched["root_cause"]
            rc_class = enriched["class"]
            confidence = enriched["confidence"]
            actions = enriched["actions"]
            reasoning = enriched["reasoning"]
            method = enriched["method"]
        except Exception as e:
            if _PROM:
                LLM_FAIL.labels(reason=type(e).__name__).inc()
            logger.warning("llm enrich failed, fallback to graph",
                           extra={"extra": {"err": str(e)}})
            method = "graph-only-fallback"
    timings["llm_ms"] = round((time.perf_counter() - t2) * 1000, 2)

    return {
        "clusters": clusters,
        "root_cause": {
            "service": root_cause_svc,
            "class": rc_class,
            "confidence": confidence,
            "reasoning": reasoning,
        },
        "recommended_actions": actions,
        "similar_incidents": similar,
        "method": method,
        "timings": timings,
    }


# --------------------------------------------------------------------------- #
# 9. FastAPI app + latency middleware
# --------------------------------------------------------------------------- #
app = FastAPI(title="AIOps Incident Service", version=APP_VERSION)

if _PROM:
    app.mount("/metrics", make_asgi_app())


@app.middleware("http")
async def latency_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    dur_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Response-Time-Ms"] = f"{dur_ms:.2f}"
    logger.info(
        "request",
        extra={"extra": {
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round(dur_ms, 2),
        }},
    )
    return response


# --------------------------------------------------------------------------- #
# 10. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz():
    """Liveness — process còn sống. Không check dependency."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Readiness — sẵn sàng phục vụ request thật. Check downstream state ĐÃ load."""
    checks = {
        "graph": GRAPH.number_of_nodes() > 0,
        "history": len(HISTORY) > 0,
    }
    # Lưu ý: KHÔNG check LLM ở đây. LLM là enrichment optional + có fallback,
    # nên LLM down KHÔNG được làm pod bị mark not-ready (tránh false outage).
    if not all(checks.values()):
        raise HTTPException(status_code=503, detail=checks)
    return {"status": "ready", "checks": checks}


@app.get("/version")
def version():
    return {
        "app": APP_VERSION,
        "graph_loaded_at": GRAPH_LOADED_AT,
        "graph_node_count": GRAPH.number_of_nodes(),
        "graph_edge_count": GRAPH.number_of_edges(),
        "history_count": len(HISTORY),
        "pipeline_config": PIPELINE_CONFIG,
    }


@app.post("/incident", response_model=IncidentResponse)
def incident(req: IncidentRequest, response: JSONResponse = None):
    # Empty list hợp lệ về type nhưng vô nghĩa -> 400 (Pydantic đã chặn sai type -> 422).
    if not req.alerts:
        raise HTTPException(status_code=400, detail="alerts must be non-empty")

    alerts = [a.model_dump() for a in req.alerts]

    t0 = time.perf_counter()
    try:
        result = process_batch(alerts)
    except Exception:
        # Không bao giờ leak stack trace ra client.
        logger.error("process_batch failed", exc_info=True)
        if _PROM:
            REQS.labels(status="error").inc()
        raise HTTPException(status_code=500, detail="internal pipeline error")

    total_ms = round((time.perf_counter() - t0) * 1000, 2)
    timings = result.pop("timings", {})
    timings["pipeline_total_ms"] = total_ms

    if _PROM:
        REQS.labels(status="success").inc()
        LAT.observe(total_ms / 1000.0)
        CLUSTERS.observe(len(result["clusters"]))

    rc = result["root_cause"]
    return IncidentResponse(
        clusters=result["clusters"],
        root_cause=RootCause(
            service=rc["service"],
            **{"class": rc["class"]},
            confidence=rc["confidence"],
            reasoning=rc["reasoning"],
        ),
        recommended_actions=result["recommended_actions"],
        similar_incidents=result["similar_incidents"],
        method=result["method"],
        meta={"timings": timings, "config": PIPELINE_CONFIG},
    )
