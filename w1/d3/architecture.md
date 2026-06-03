# E2E Data Layer Architecture: Payment Service Anomaly Detection

Tài liệu thiết kế kiến trúc hệ thống dữ liệu (Data Layer Architecture) phục vụ bài toán **phát hiện bất thường (Anomaly Detection) trên Payment Service**. 

Hệ thống đi theo luồng chuẩn: **Service → Collection → Transport → Processing → Storage → Query/ML**.

---

## 1. Sơ đồ Kiến trúc Tổng thể

Sơ đồ mô tả luồng di chuyển của Metrics, Logs, Traces và cách dữ liệu được xử lý real-time phục vụ ML:

![E2E Payment Service Telemetry Architecture Diagram](architecture.png)

---

## 2. Chi tiết Lựa chọn Công cụ (Component Breakdown)

| Component | Công cụ lựa chọn | Vai trò & Giải pháp cụ thể | Lý do lựa chọn & Trade-off |
| :--- | :--- | :--- | :--- |
| **1. Service** | **OpenTelemetry SDK** | Nhúng trực tiếp vào code Payment Service để emit: HTTP rate, latency, error rate, DB pool metrics, và Traces. | **Chuẩn hóa (Vendor-neutral)**: Code một lần, dễ dàng xuất dữ liệu sang bất kỳ backend nào (VM, ES, Datadog) không cần đổi code. |
| **2. Collection**| **OpenTelemetry Collector**| Chạy dạng **DaemonSet** trên các node Kubernetes để gom, batch, lọc logs health check trước khi gửi đi. | **Hiệu năng cao**: Nhẹ hơn Fluentd, xử lý lọc nhiễu ngay tại Edge/Node giúp tiết kiệm băng thông mạng. |
| **3. Transport** | **Apache Kafka** | Buffer trung gian (Message Queue). Chia thành các topic: `payment-metrics`, `payment-logs`, `payment-traces`. | **Giảm tải (Backpressure)**: Tránh làm sập đống DB phía sau khi traffic tăng đột biến. <br>*Trade-off*: Tăng ~5-15ms độ trễ và tốn công vận hành cluster. |
| **4. Processing**| **Apache Flink** | Đọc stream từ Kafka. Parse log thô sang JSON, tính latency p99 (sliding window 1 phút) và đẩy features vào Redis. | **Stateful Stream Processing**: Xử lý real-time thực sự trên từng event, quản lý state tốt hơn Spark Streaming chạy mini-batch. |
| **5. Storage** | **VictoriaMetrics, ES, Loki, Redis, S3** | Lưu trữ phân tầng (**Tiered Storage**):<br>- **VM**: Lưu metrics.<br>- **ES**: Lưu hot logs lỗi (7 ngày).<br>- **Loki**: Lưu warm logs thô trên S3.<br>- **Redis**: Làm online Feature Store cho ML.<br>- **S3**: Lưu cold logs nén Parquet. | **Tối ưu chi phí**: S3 Standard ($0.023/GB) và Loki rẻ hơn ES (lưu trên EBS $0.08/GB) tới 75% chi phí lưu trữ log thô dài hạn. |
| **6. Query / ML** | **Grafana, Alertmanager, Python ML Worker** | - **Grafana**: Dashboards trực quan.<br>- **ML Worker**: Chạy script Isolation Forest lấy features từ Redis để detect anomaly. | **Phát hiện bất thường đa biến**: Isolation Forest gom các metrics (latency, error rate, CPU) để phát hiện bất thường kết hợp. |

---

## 3. Luồng Xử lý Sự cố (Incident Workflow)

Quy trình phát hiện lỗi (Ví dụ: cổng thanh toán Stripe bị chậm gây nghẽn kết nối và tăng latency trên Payment Service):

![Incident Detection Sequence Flowchart](sequence.png)

---

## 4. Phân tích Các Quyết định Thiết kế (Architectural Trade-offs)

### A. Kafka vs Direct Push (Đẩy thẳng đến Storage)
- **Quyết định**: Dùng Kafka làm đệm.
- **Trade-off**: Đảm bảo an toàn dữ liệu khi DB bị quá tải (backpressure). Flink có thể đọc ghi song song mà không ảnh hưởng tới DB. Đổi lại, tăng 5-15ms độ trễ và tốn chi phí hạ tầng chạy Kafka.

### B. VictoriaMetrics vs Prometheus
- **Quyết định**: Dùng VictoriaMetrics (VM).
- **Trade-off**: VM tốn ít hơn ~70% RAM và ~50% Disk IOPS so với Prometheus khi xử lý lượng lớn active time-series (high cardinality). Tương thích hoàn toàn với PromQL của Grafana.

### C. Tiered Storage cho Log (Elasticsearch vs Loki)
- **Quyết định**: Dùng Elasticsearch cho Hot logs (7 ngày) và Loki + S3 cho Warm logs (30 ngày).
- **Trade-off**: Tiết kiệm 75% chi phí lưu trữ logs. Đổi lại, khi search logs cũ hơn 7 ngày sẽ chậm hơn (mất 150ms-2s) và dev phải học cách dùng LogQL của Loki.
