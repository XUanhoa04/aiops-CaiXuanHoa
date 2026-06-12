# SUBMIT.md — AIOps W2-D3 Model Serving (HOA)

## Nộp bài

- **Path:** `aiops-hoa/w2/d3/`
- **Files:** `serve.py`, `DESIGN.md`, `SUBMIT.md`
- **Run:** `uvicorn serve:app --port 8000 --workers 1` (máy yếu OK — single process, RAM idle ~150 MB)
- **Benchmark LLM-bypass:** `AIOPS_USE_LLM=false` (default) để đo concurrency không lệ thuộc external API.

## Acceptance — đã verify (chạy `py _smoke_test.py`, exit 0)

| Tiêu chí | Kết quả |
|----------|---------|
| `uvicorn serve:app --port 8000 --workers 1` | chạy được, startup complete |
| `GET /healthz` | `200 {"status":"ok"}` |
| `POST /incident` valid input | `200`, body có `clusters`, `root_cause`, `recommended_actions` |
| Invalid input `{"alerts":[{"id":"x"}]}` | `422` (không 500) |
| Empty `{"alerts":[]}` | `400` |
| Pipeline end-to-end thật | `correlate` (d1) + `rca_pipeline` (d2) import thật, không mock |

Output thật trên dataset 20 alert: `root_cause = payment-svc`, `class = connection_pool_exhaustion`,
`confidence = 0.9`, retrieval match 3 incident history (đúng kỳ vọng INC payment pool exhaustion).

---

## EOD Checkpoint — 3 câu (dựa trên số đo thật)

### 1. Latency thực của endpoint

Chạy 20 request liên tiếp với dataset 20 alert thật, đo từ header `X-Response-Time-Ms`:

```
20-req sequential:  p50 = 3.84 ms   p99 = 4.95 ms   min = 2.34   max = 47.42
request đầu (cold):              253 ms
per-phase (warm):   correlate ≈ 1.05 ms | rca ≈ 3–4 ms | llm = 0 ms (disabled)
```

**Phase chiếm phần lớn:** RCA. Ở request đầu (cold) RCA ngốn ~250 ms do `networkx.pagerank` +
import warmup; sau khi warm, RCA vẫn là phần lớn nhất trong tổng (~3–4 ms) còn correlate chỉ ~1 ms.
Vì `AIOPS_USE_LLM=false` nên phase LLM = 0; nếu bật, LLM sẽ dominate (~91%) như note trong §5.

**Scale khi input ×10:**
- **Linear theo số alert:** validate, serialize, và phần session-window của correlate.
- **Không linear / theo số service:** topology-group là O(S²) theo số *service* (S nhỏ và bị chặn),
  PageRank theo size subgraph — nên ×10 *alert* gần như không làm RCA tăng tuyến tính.
- **Fixed cost:** load GRAPH/HISTORY (chỉ chạy 1 lần lúc startup), và LLM call (fixed ~vài giây/call
  bất kể alert count, vì chỉ enrich primary cluster).

### 2. LLM down / 4 request đồng thời — endpoint handle ra sao

Test concurrency Windows (`ThreadPoolExecutor`, tương đương `ab -n 20 -c 4`):

```
n=20  concurrency=4
status: {200: 20}   errors: 0 (0.0%)
wall   p50 = 31.63 ms   p99 = 316.14 ms
server p50 =  8.59 ms   p99 = 270.72 ms
throughput: 44.0 req/s
```

**Bottleneck đầu tiên quan sát được:** với `--workers 1`, endpoint là `def` (sync) nên FastAPI chạy
nó trong threadpool — concurrency bị giới hạn bởi số thread của threadpool + GIL khi phần CPU-bound
(PageRank) chạy. Request đầu cold (~270 ms server-side) kéo p99 lên, các request sau warm xuống ~8 ms.
Không có lỗi nào ở c=4; bottleneck thật chỉ xuất hiện khi concurrency cao hơn nhiều hoặc khi bật LLM
(lúc đó mỗi request giữ thread chờ I/O vài giây → threadpool cạn).

**Fallback path khi LLM down:** có. `_llm_enrich` được gọi trong try/except — khi provider down,
timeout (`timeout=10.0`), hoặc trả JSON sai/hallucinate, nó raise → endpoint fallback về graph+retrieval
output (`method="graph-only-fallback"`), inc `aiops_llm_failures_total`, **vẫn trả 200**. Ngoài ra
`AIOPS_USE_LLM=false` là kill switch: set env + restart 30s là bypass LLM hoàn toàn, không cần redeploy.

### 3. `/healthz` vs `/readyz` — check gì, vì sao tách

- **`/healthz` (liveness):** chỉ trả `{"status":"ok"}`, không check dependency. Trả lời câu hỏi
  "process còn sống không?" — dùng cho load balancer / k8s liveness probe để biết có cần restart pod.
- **`/readyz` (readiness):** check downstream state đã load — `graph` (`GRAPH.number_of_nodes() > 0`)
  và `history` (`len(HISTORY) > 0`). Trả lời "đã sẵn sàng nhận request thật chưa?". Fail → `503`,
  k8s remove pod khỏi rotation.

**Vì sao tách 2 endpoint:** chúng trả lời 2 câu hỏi khác nhau. Một pod có thể *alive* (process chạy)
nhưng *not ready* (graph chưa load xong lúc startup) — nếu gộp 1, load balancer sẽ restart pod thay
vì chỉ tạm thời không route traffic vào, gây restart loop vô nghĩa.

**Khi LLM API down, `/readyz` của mình vẫn PASS.** Lý do: LLM là enrichment **optional có fallback**
(graph-only vẫn cho output chất lượng tốt). Nếu để `/readyz` fail khi OpenAI down, k8s sẽ mark toàn
bộ pod not-ready → false outage cho cả service trong khi pipeline thực ra vẫn phục vụ được. Readiness
chỉ nên phụ thuộc dependency *bắt buộc* (graph, history), không phụ thuộc dependency *optional* (LLM).

---

## Reflection

Điều rút ra rõ nhất: "đo trước, optimize sau" là thật. Trước khi đo mình tưởng correlate hay
serialize tốn thời gian, nhưng số liệu chỉ ra RCA (PageRank) mới là phần lớn, và cost lớn nhất thực
ra là **cold start** (~250 ms request đầu) chứ không phải steady-state (~4 ms). Bài học thứ hai là
ranh giới readiness: việc cố tình *không* check LLM trong `/readyz` là quyết định production quan
trọng — phân biệt dependency bắt buộc vs optional quyết định service có bị false-outage hay không.
Feature flag + fallback path biến "LLM provider down" từ sự cố P1 thành một dòng log warning.

> Test phụ trợ: `_smoke_test.py` (acceptance + latency p50/p99) và `_concurrency_test.py`
> (`-c 4`, ThreadPoolExecutor) — đều reproducible.
