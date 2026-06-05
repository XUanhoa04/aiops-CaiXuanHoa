import json
import os
import sys
from datetime import datetime, timezone
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()
# Set ALERTS_FILE relative to pipeline.py's directory
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ALERTS_FILE = os.path.join(CURRENT_DIR, "alerts.jsonl")

# Stateful variables to track stream status
state = {
    "anomaly_detected": False,
    "detected_type": None,
    "memory_leak_consecutive": 0,
    "traffic_spike_consecutive": 0,
    "dependency_timeout_consecutive": 0,
    "last_timestamp": None,
}

def write_alert(timestamp: str, fault_type: str, message: str, severity: str = "critical"):
    alert = {
        "timestamp": timestamp,
        "type": fault_type,
        "severity": severity,
        "message": message
    }
    # Ensure alerts.jsonl exists or append to it
    with open(ALERTS_FILE, "a") as f:
        f.write(json.dumps(alert) + "\n")
    print(f"[ALERT FIRED] timestamp={timestamp}, type={fault_type}, message='{message}'")

def reset_state():
    state["anomaly_detected"] = False
    state["detected_type"] = None
    state["memory_leak_consecutive"] = 0
    state["traffic_spike_consecutive"] = 0
    state["dependency_timeout_consecutive"] = 0
    state["last_timestamp"] = None
    print("[INFO] Stream detection state reset.")

@app.post("/ingest")
async def ingest(request: Request):
    payload = await request.json()
    metrics = payload.get("metrics", {})
    logs = payload.get("logs", [])
    timestamp = payload.get("timestamp")

    # Detect generator restarts (e.g. timestamp jumps backward or missing timestamp)
    if timestamp and state["last_timestamp"]:
        try:
            current_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            last_time = datetime.fromisoformat(state["last_timestamp"].replace("Z", "+00:00"))
            if current_time < last_time:
                print("[INFO] Timestamp jumped backward. Resetting state.")
                reset_state()
        except Exception as e:
            pass

    state["last_timestamp"] = timestamp

    # Extract metrics
    mem_usage = metrics.get("memory_usage_bytes", 0)
    cpu_usage = metrics.get("cpu_usage_percent", 0.0)
    rps = metrics.get("http_requests_per_sec", 0.0)
    latency = metrics.get("http_p99_latency_ms", 0.0)
    err_rate = metrics.get("http_5xx_rate", 0.0)
    gc_pause = metrics.get("jvm_gc_pause_ms_avg", 0.0)
    queue = metrics.get("queue_depth", 0)
    upstream_timeout = metrics.get("upstream_timeout_rate", 0.0)

    # Check logs for direct indicator keywords
    log_has_memory_leak = False
    log_has_traffic_spike = False
    log_has_dependency_timeout = False

    for log in logs:
        msg = log.get("message", "")
        # Memory leak keywords
        if "GC pause exceeded threshold" in msg or "OutOfMemory" in msg:
            log_has_memory_leak = True
        # Traffic spike keywords
        if "overloaded" in msg or "Queue depth high" in msg:
            log_has_traffic_spike = True
        # Dependency timeout keywords
        if "Circuit breaker OPEN" in msg or "Upstream timeout" in msg:
            log_has_dependency_timeout = True

    # 1. Memory Leak detection
    # Normal range: ~800MB. If it is > 950MB or we see log indicator
    is_mem_leak = (mem_usage > 950_000_000) or log_has_memory_leak

    # 2. Dependency Timeout detection
    # Normal range: 0 - 0.4%. Timeout rate > 1.5% is anomalous.
    is_dep_timeout = (upstream_timeout > 1.5) or log_has_dependency_timeout

    # 3. Traffic Spike detection
    # Normal rps: 80 - 160. RPS > 220 is anomalous. (Make sure upstream_timeout is low to avoid overlap)
    is_traffic_spike = ((rps > 220 and upstream_timeout < 0.8) or log_has_traffic_spike)

    # Track consecutive ticks to avoid false alarms on transient noise
    if is_mem_leak:
        state["memory_leak_consecutive"] += 1
    else:
        state["memory_leak_consecutive"] = 0

    if is_dep_timeout:
        state["dependency_timeout_consecutive"] += 1
    else:
        state["dependency_timeout_consecutive"] = 0

    if is_traffic_spike:
        state["traffic_spike_consecutive"] += 1
    else:
        state["traffic_spike_consecutive"] = 0

    # Fire alert if threshold reached and not already alerted
    if not state["anomaly_detected"]:
        if state["memory_leak_consecutive"] >= 2:
            state["anomaly_detected"] = True
            state["detected_type"] = "memory_leak"
            msg = f"Memory usage growing abnormally: {mem_usage / 1_000_000:.1f} MB (limit 2000 MB), GC pause avg: {gc_pause:.1f} ms"
            write_alert(timestamp, "memory_leak", msg, "critical")
        elif state["dependency_timeout_consecutive"] >= 2:
            state["anomaly_detected"] = True
            state["detected_type"] = "dependency_timeout"
            msg = f"Upstream dependency timeout rate is high: {upstream_timeout}%, http 5xx rate: {err_rate}%, latency: {latency:.1f} ms"
            write_alert(timestamp, "dependency_timeout", msg, "critical")
        elif state["traffic_spike_consecutive"] >= 2:
            state["anomaly_detected"] = True
            state["detected_type"] = "traffic_spike"
            msg = f"Sudden traffic surge detected: {rps} rps, queue depth: {queue}, cpu: {cpu_usage:.1f}%"
            write_alert(timestamp, "traffic_spike", msg, "critical")

    return {"status": "ok"}

if __name__ == "__main__":
    # Allow port selection via env var or command line args
    port = 8000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    elif os.environ.get("PORT"):
        try:
            port = int(os.environ.get("PORT"))
        except ValueError:
            pass

    print(f"[INFO] Starting anomaly detection pipeline on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
