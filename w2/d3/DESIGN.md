# DESIGN.md — AIOps W2-D3 Incident Serving (HOA)

## 1. Pipeline architecture trong endpoint

`POST /incident` chạy pipeline end-to-end thật, không mock:

```
raw alerts (JSON)
  → validate            (Pydantic IncidentRequest / Alert — 8 field/alert)
  → correlate()         [Layer 1, import thật từ w2/d1/correlate.py]
                          session-window (gap_sec) → topology-group (max_hop, Union-Find)
  → pick primary        (cluster có alert_count lớn nhất)
  → rca_pipeline()      [Layer 2, import thật từ w2/d2/rca.py]
                          PageRank + temporal scorer → retrieval kNN trên 29 incident history
  → (optional) LLM      (gated bằng AIOPS_USE_LLM, có cache + timeout + fallback)
  → serialize           → IncidentResponse (clusters, root_cause, recommended_actions, ...)
```

Service graph (`GRAPH`, 14 node / 17 edge) và incident history (`HISTORY`, 29 incident) được
load **một lần lúc import** (module-level state), không reload mỗi request — `build_graph()` và
`networkx.pagerank` là cost cố định nên cache ở startup là quyết định đúng. Endpoint là `def`
(sync) nên FastAPI tự đẩy vào threadpool, giữ event loop free cho health-check song song.

## 2. Latency budget breakdown (đo thật, dataset 20 alert)

| Phase     | Warm (đo) | Tính chất khi input ×10 |
|-----------|-----------|--------------------------|
| validate  | < 1 ms    | linear theo số alert (rẻ) |
| correlate | ~1.05 ms  | gần linear; topology-group là O(S²) theo **số service** (S nhỏ), không phải số alert |
| RCA       | ~3–4 ms warm (250 ms cold lần đầu do PageRank warmup) | dominate; PageRank trên subgraph + retrieval O(history) |
| LLM       | 0 ms (AIOPS_USE_LLM=false) | nếu bật → ~91% tổng latency, fixed cost ~vài giây/call |
| serialize | < 1 ms    | linear theo cluster count |

Sequential 20-req: **p50 = 3.84 ms, p99 = 4.95 ms** (request đầu cold = 253 ms vì networkx warmup).
Khi bật LLM, budget dịch hoàn toàn sang LLM call — đó là lý do có cache (TTL 1h) + skip-LLM khi
graph confidence cao + timeout 10s.

## 3. Production concern: fault tolerance khi LLM provider down

LLM được wrap bằng feature flag `AIOPS_USE_LLM` (kill switch) **và** try/except với fallback path.
Khi provider outage hoặc trả JSON sai/hallucinate (root_cause không nằm trong cluster services),
`_llm_enrich` raise → endpoint fallback về graph+retrieval output với `method="graph-only-fallback"`,
inc counter `aiops_llm_failures_total`, và **vẫn trả 200**. Mọi outbound call có `timeout=10.0,
max_retries=2` để tránh hang làm cạn connection pool. Quyết định cụ thể: **`/readyz` KHÔNG check
LLM** — vì LLM là enrichment optional có fallback, nếu mark pod not-ready khi OpenAI down sẽ tạo
false outage cho toàn bộ service trong khi pipeline graph-only vẫn phục vụ tốt.

## 4. Trade-off: vì sao FastAPI thay vì Flask / BentoML

- **vs Flask:** pipeline có LLM call (IO-bound, hưởng async) và input có schema 8 field. FastAPI
  cho Pydantic validation native → input sai tự trả **422 chứ không 500** mà không phải viết tay
  if/else như Flask. Đã verify: `{"alerts":[{"id":"x"}]}` → 422, `{"alerts":[]}` → 400.
- **vs BentoML:** BentoML là model-centric (versioning/batching cho ML model). Workload ở đây là
  **glue pipeline** (graph + retrieval + optional LLM), không phải một ML model đơn lẻ, nên overhead
  và learning curve của BentoML không đáng. FastAPI standalone đủ nhẹ (~50 MB dep, RAM idle ~150 MB,
  chạy `--workers 1` trên máy 4 GB).

## 5. Concrete decisions

- **`gap_sec = 120s`**: chọn 120s vì cascade trong GeekShop thường lan trong < 2 phút; gap nhỏ hơn
  (30s) tách cascade thành nhiều cluster rời, lớn hơn (300s) gộp nhầm hai sự cố độc lập.
- **`max_hop = 2`**: đủ bắt quan hệ caller→callee→store (vd gateway→payment-svc→payment-db) mà
  không nối nhầm service cách xa nhau về topo.
- **primary = cluster lớn nhất theo `alert_count`**: cluster nhiều alert nhất là incident có blast
  radius lớn nhất, ưu tiên RCA trước.
- Config override được qua env (`AIOPS_GAP_SEC`, `AIOPS_MAX_HOP`) và phơi ra `/version` để khi
  correlation regress có thể kiểm tra config trước khi đổ tại code.
