"""Smoke test + 20-request latency benchmark for serve.py (run with: py _smoke_test.py)."""
import json
import statistics
from pathlib import Path

from fastapi.testclient import TestClient
import serve

client = TestClient(serve.app)

HERE = Path(__file__).resolve().parent
ALERTS = HERE.parent / "d2" / "dataset" / "alerts_sample.jsonl"


def load_alerts():
    out = []
    with open(ALERTS, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def main():
    # 1. healthz
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}, r.text
    print("healthz:", r.json())

    # 2. readyz
    r = client.get("/readyz")
    assert r.status_code == 200, r.text
    print("readyz:", r.json())

    # 3. version
    print("version:", client.get("/version").json())

    # 4. invalid input -> 422 (missing required fields), not 500
    r = client.post("/incident", json={"alerts": [{"id": "x"}]})
    assert r.status_code == 422, f"expected 422 got {r.status_code}: {r.text}"
    print("invalid-input status:", r.status_code)

    # 5. empty alerts -> 400
    r = client.post("/incident", json={"alerts": []})
    assert r.status_code == 400, r.text
    print("empty-alerts status:", r.status_code)

    # 6. valid -> 200 with required keys
    alerts = load_alerts()
    body = {"alerts": alerts}
    r = client.post("/incident", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    for k in ("clusters", "root_cause", "recommended_actions"):
        assert k in data, f"missing {k}"
    print("incident root_cause:", data["root_cause"])
    print("incident method:", data["method"], "| actions:", data["recommended_actions"])
    print("incident timings:", data["meta"]["timings"])

    # 7. 20 sequential requests -> p50/p99 from X-Response-Time-Ms
    lat = []
    for _ in range(20):
        resp = client.post("/incident", json=body)
        lat.append(float(resp.headers["X-Response-Time-Ms"]))
    lat.sort()
    p50 = statistics.median(lat)
    p99 = lat[int(0.99 * (len(lat) - 1))]
    print(f"\n20-req latency  p50={p50:.2f}ms  p99={p99:.2f}ms  min={lat[0]:.2f}  max={lat[-1]:.2f}")
    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
