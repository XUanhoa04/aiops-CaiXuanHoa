# SUBMIT.md — W2/D1 Alert Correlation Lab

## 7.3 Assignment Answers

### gap_sec = 120, vì sao?

Chọn `gap_sec = 120` (2 phút) vì đây là sweet spot cho production burst detection. Trong dataset 20 alert, tất cả alert xảy ra trong khoảng ~6.5 phút (09:42:01 → 09:48:30), với max gap giữa 2 alert liên tiếp chỉ 49 giây (a-0015 → a-0016 và a-0017 → a-0018). Với `gap_sec=120`, tất cả alert thuộc cùng 1 session — phản ánh đúng thực tế rằng đây là 1 incident kéo dài, không phải nhiều incident riêng biệt.

**Trade-off**: Nếu giảm `gap_sec` xuống 35-40s, dataset sẽ bị split thành 3-4 session, tách rời các alert thuộc cùng 1 cascade chain (ví dụ: payment-svc alert đầu ở session 0 và payment-svc alert cuối ở session 3). Điều này dẫn đến false separation — cùng 1 root cause nhưng bị chia thành nhiều cluster.

### max_hop = 2, vì sao?

Chọn `max_hop = 2` vì trong service mesh, cascade thường lan qua 2 hop: service A → dependency B → dependency C. Ví dụ: edge-lb (hop 1) → checkout-svc (hop 2) → payment-svc. Với `max_hop=2`, các service trên cùng cascade chain được gom lại.

**Hậu quả thực tế**: Với dataset này, `max_hop=2` gom TẤT CẢ 7 alerting service thành 1 cluster vì graph nhỏ và kết nối dày đặc. `recommender-svc` cách `edge-lb` chỉ 2 hop (qua `catalog-svc`), nên bị kéo vào cluster chính dù thực tế nó là batch retrain không liên quan. Đây là limitation rõ ràng nhất.

### 1 alert ID bị "miss" — tại sao?

**Alert a-0013** (`recommender-svc|cpu_utilization|warn`) là alert bị "miss" về mặt logic. Nó được gom vào cluster chính (payment cascade) nhưng thực tế là **unrelated** — nó là batch retrain chạy đồng thời, có label `note: "unrelated — concurrent batch retrain"`.

Correlator "miss" nó vì:
1. **Time**: a-0013 nằm trong cùng time session (gap tới alert trước/sau < 120s)
2. **Topology**: `recommender-svc` cách `edge-lb` chỉ 2 hop trên undirected graph (qua `catalog-svc`), nên Union-Find gom nó vào cluster chính

Correlator thiếu context semantic — nó chỉ biết "gần nhau về thời gian + gần nhau trên graph" nhưng không biết alert này có **cùng root cause** hay không.

Tương tự, **a-0016** (`search-svc|catalog_db_query_time_ms|warn`) cũng bị false correlation — nó là independent slow query, nhưng `search-svc` cách `edge-lb` chỉ 1 hop nên bị gom vào.

### Nếu có 10,000 alert thay vì 20, code chậm ở đâu?

1. **`topology_group` — O(n² × PathLookup)**: Với N alerting services trong 1 session, cặp đôi comparison = N×(N-1)/2. Mỗi cặp gọi `nx.shortest_path_length()` = BFS O(V+E). Nếu 10,000 alert từ 500 service → 124,750 BFS calls — bottleneck chính.
2. **`session_groups` — O(n log n)**: Sort 10,000 alert là trivial.
3. **Memory**: Dedup store với 10,000 alert tạo hàng ngàn fingerprint entries — cần TTL eviction.
4. **Union-Find**: Bản thân Union-Find là near O(1) per operation, nhưng số lần gọi = O(n²) theo cặp service.

**Giải pháp**: Pre-compute all-pairs shortest path 1 lần bằng `nx.all_pairs_shortest_path_length()`, cache kết quả. Hoặc dùng BFS flood-fill thay vì pairwise comparison.

---

## 8. EOD Checkpoint

### Câu 1: Vì sao fingerprint không include timestamp hay value?

Fingerprint dùng để nhận diện "cùng 1 loại alert". Nếu include `timestamp`, mỗi lần alert fire ở thời điểm khác nhau sẽ tạo fingerprint khác → 2 alert cùng `payment-svc|latency_p99_ms|crit` fire cách nhau 30 giây sẽ thành 2 fingerprint khác nhau → dedup hoàn toàn mất tác dụng. Tương tự với `value`: latency = 1840ms lần 1 và 1920ms lần 2 sẽ tạo 2 fingerprint dù cùng 1 hiện tượng.

**Ví dụ**: Trong dataset, a-0003 (value=1840), a-0008 (value=1840), a-0015 (value=1840) cùng là `payment-svc|latency_p99_ms|crit`. Nếu include value, chúng vẫn match (trùng 1840). Nhưng nếu value dao động (1840 → 1920 → 1760), chúng sẽ thành 3 fingerprint riêng biệt — dedup ratio giảm từ 3→1 xuống 3→3 (vô dụng).

### Câu 2: "Duplicate" vs "Correlated" alert

- **Duplicate**: 2+ alert có **cùng fingerprint** (cùng service + metric + severity). Chúng là cùng 1 "loại" alert fire lặp lại. Ví dụ: `a-0003`, `a-0008`, `a-0015` đều là `payment-svc|latency_p99_ms|crit` — 3 lần fire cùng 1 rule.

- **Correlated**: 2+ alert **khác fingerprint** nhưng có **cùng root cause**. Chúng khác service hoặc khác metric nhưng xảy ra gần nhau + trên cùng dependency chain. Ví dụ: `a-0003` (`payment-svc|latency_p99_ms|crit`) và `a-0006` (`checkout-svc|downstream_payment_error_rate|crit`) — khác service, khác metric, nhưng cùng cause: payment-svc chậm → checkout báo downstream error.

**Tóm lại**: Duplicate = giống hệt nhau, Correlated = khác nhau nhưng liên quan.

### Câu 3: gap_sec = 30 vs gap_sec = 600

- **gap_sec = 30** (rất ngắn): Dataset bị split thành 4-5 session nhỏ (vì có 3 gap > 30s: 40s, 49s, 49s). Alert cùng 1 incident bị tách thành nhiều cluster — false separation, mất context cascade dài.

- **gap_sec = 600** (rất dài): Tất cả alert trong 10 phút đều thuộc 1 session. Nếu có 2 incident hoàn toàn khác nhau xảy ra cách nhau 5 phút, chúng vẫn bị merge — false correlation, noise tăng.

### Câu 4: Recommender-svc có bị gom vào cluster chính không?

**Có**, correlator gom `recommender-svc` (a-0013) vào cluster chính. Lý do:

1. **Time**: a-0013 (09:45:10) nằm trong session gap 120s so với alert trước/sau
2. **Topology**: `recommender-svc` cách `edge-lb` chỉ 2 hop trên undirected graph: `recommender-svc ↔ catalog-svc ↔ edge-lb`. Với `max_hop=2`, Union-Find gom chúng vào cùng set. Từ đó, through transitivity, `recommender-svc` join cluster chứa `edge-lb`, `checkout-svc`, `payment-svc`, v.v.

**Đây là false correlation** — recommender chạy batch retrain hoàn toàn không liên quan đến payment pool exhaustion. Nhưng correlator chỉ biết "gần về thời gian + gần trên graph" → nó không có khả năng phân biệt.

### Câu 5: Limitation lớn nhất của topology grouping

**Limitation**: Topology grouping coi mọi edge là equivalent — nó không phân biệt giữa **direct dependency edge** (checkout-svc → payment-svc, synchronous HTTP call) và **indirect/tangential edge** (catalog-svc → recommender-svc, batch job). Kết quả: service chỉ "hàng xóm" trên graph nhưng không có causality relationship vẫn bị gom chung.

Trong dataset: `recommender-svc` nằm cạnh `catalog-svc` trên graph (1 hop), và `catalog-svc` nằm cạnh `edge-lb` (1 hop). Qua Union-Find transitivity, recommender bị kéo vào cluster payment cascade dù không có quan hệ nhân quả.

**Cách khắc phục**: Thêm **edge weight** dựa trên criticality + type. Ví dụ: `http` synchronous edge (checkout → payment) có weight = 1, `kafka` async edge có weight = 2, `batch` edge có weight = 5. Khi tính khoảng cách, dùng weighted shortest path thay vì hop count. Điều này khiến recommender-svc "xa" hơn trên weighted graph, giảm false correlation. Ngoài ra có thể thêm **directed cascade analysis** — chỉ group services nằm trên cùng directed dependency chain thay vì undirected distance.
