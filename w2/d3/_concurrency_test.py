"""Concurrency benchmark — 20 requests, 4 concurrent (Windows-friendly, ab-equivalent).

Run AFTER `uvicorn serve:app --port 8000 --workers 1` is up.
    py _concurrency_test.py
"""
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ALERTS = HERE.parent / "d2" / "dataset" / "alerts_sample.jsonl"
URL = "http://127.0.0.1:8000/incident"


def load_alerts():
    out = []
    with open(ALERTS, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def one_call(body):
    t0 = time.perf_counter()
    r = httpx.post(URL, json=body, timeout=30.0)
    wall = (time.perf_counter() - t0) * 1000
    server = float(r.headers.get("X-Response-Time-Ms", "0"))
    return r.status_code, wall, server


def main():
    body = {"alerts": load_alerts()}
    n, concurrency = 20, 4
    results = []
    t_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(one_call, body) for _ in range(n)]
        for f in futs:
            results.append(f.result())
    total_wall = time.perf_counter() - t_start

    statuses = [r[0] for r in results]
    walls = sorted(r[1] for r in results)
    servers = sorted(r[2] for r in results)
    errors = sum(1 for s in statuses if s != 200)

    def pct(xs, p):
        return xs[int(p * (len(xs) - 1))]

    print(f"n={n} concurrency={concurrency}")
    print(f"status codes: {dict((s, statuses.count(s)) for s in set(statuses))}")
    print(f"errors: {errors} ({errors/n*100:.1f}%)")
    print(f"wall   p50={statistics.median(walls):.2f}ms p99={pct(walls,0.99):.2f}ms "
          f"min={walls[0]:.2f} max={walls[-1]:.2f}")
    print(f"server p50={statistics.median(servers):.2f}ms p99={pct(servers,0.99):.2f}ms "
          f"min={servers[0]:.2f} max={servers[-1]:.2f}")
    print(f"throughput: {n/total_wall:.1f} req/s  (total {total_wall*1000:.1f}ms)")


if __name__ == "__main__":
    main()
