# Báo cáo Bài tập: Data Layer Architecture & Cost Optimization (W1-D3)

Báo cáo này tổng hợp kết quả của cả 3 Phase: Thiết kế kiến trúc E2E, Đánh giá chi phí (Cost Estimation) và Hồ sơ quyết định kiến trúc (ADR) cho hệ thống giám sát Payment Service.

---

## 1. Sơ đồ Kiến trúc Hệ thống dữ liệu E2E (Architecture)

Kiến trúc luồng dữ liệu cho use case **Anomaly Detection trên Payment Service** được mô tả chi tiết tại file [architecture.md](./architecture.md).

### Sơ đồ luồng dữ liệu:
![E2E Payment Service Telemetry Architecture Diagram](architecture.png)

---

## 2. Bảng Ước tính Chi phí (Cost Estimation Results)

Dưới đây là kết quả phân tích chi phí từ script [cost_model.py](./cost_model.py) cho 3 quy mô (Small, Medium, Large) so sánh giữa **Build (Tự chạy trên Cloud)** và **Buy (Datadog SaaS)**:

### Bảng tổng hợp chi phí (USD/tháng):

| Quy mô (Tier) | Chi phí Tự chạy (Build) | Chi phí Datadog (Buy) | Tiết kiệm hàng tháng | Quyết định khuyến nghị |
| :--- | :---: | :---: | :---: | :--- |
| **Small** (10 Services, 15 Hosts) | $2,829.81 | $4,360.00 | $1,530.19 | **Buy (Datadog)** (Xem phần Reflection) |
| **Medium** (100 Services, 150 Hosts) | $16,308.07 | $41,100.00 | $24,791.93 | **Build (Self-Hosted)** |
| **Large** (1000 Services, 1500 Hosts) | $87,220.66 | $411,000.00 | $323,779.34 | **Build (Self-Hosted)** |

### Chi tiết phân tích chi phí theo cấu phần (Medium Tier - 150 Hosts, 500GB Logs, 1M EPS):
- **Compute / Infrastructure VM**: **$1,910.00** (Build) vs **$7,350.00** (Buy - Licenses)
- **Storage / Ingest & Indexing**: **$552.08** (Build - Lưu trữ phân tầng ES+Loki+S3) vs **$27,000.00** (Buy - Giá Datadog $1.80/GB logs)
- **Network / Custom Metrics**: **$3,845.98** (Build - Egress nhân bản dữ liệu) vs **$6,750.00** (Buy - Giá Custom Metrics)
- **Labor / Operations**: **$10,000.00** (Build - 1.0 SRE vận hành) vs **$0.00** (Buy - Zero maintenance)

---

## 3. Tóm tắt Quyết định Kiến trúc (ADR Summary)

Chi tiết hồ sơ quyết định kiến trúc nằm tại file [ADR-001.md](./ADR-001.md).

- **Quyết định**: Sử dụng mô hình **Lưu trữ logs phân tầng (Tiered Log Storage)** thay vì lưu trữ 100% logs trên Elasticsearch.
  - **Hot logs (0-7 ngày)**: Lưu trữ các logs lỗi/quan trọng trên **Elasticsearch** hỗ trợ lập chỉ mục toàn văn (full-text index) giúp kỹ sư tìm kiếm nhanh sự cố dưới 50ms.
  - **Warm logs (7-30 ngày)**: Lưu trữ toàn bộ logs thô trên **Grafana Loki** (lưu trữ thực tế trên **S3**), chỉ lập chỉ mục metadata labels nhằm tối ưu hóa chi phí.
- **Trade-offs**:
  - *Lợi ích*: Tiết kiệm **75% chi phí lưu trữ logs** (giảm từ $3,240 xuống còn $552/tháng ở quy mô Medium). ES cluster chạy nhẹ và ổn định hơn.
  - *Hạn chế*: Độ trễ khi truy vấn log cũ (Warm logs) tăng lên (từ 150ms đến 2s) và kỹ sư phát triển bắt buộc phải học thêm cú pháp LogQL để lọc theo nhãn trước khi tìm kiếm.

---

## 4. Ý kiến chuyên gia (Platform Engineer Reflection)

**Câu hỏi**: *Nếu bạn được thuê làm Platform Engineer cho một startup có 50 dịch vụ (services) vừa gọi vốn thành công vòng Series A, bạn sẽ đề xuất BUILD (tự xây dựng) hay BUY (mua giải pháp SaaS như Datadog)? Tại sao?*

### Đề xuất: BUY (Chọn giải pháp SaaS như Datadog/New Relic)

### Lý do chi tiết:

1. **Tập trung vào sản phẩm cốt lõi (Focus on Core Product)**:
   - Ở giai đoạn Series A, mục tiêu sống còn của startup là tìm kiếm **Product-Market Fit (PMF)** và nhanh chóng cải tiến sản phẩm để tăng trưởng người dùng. 
   - Vận hành một hệ thống tự chạy (Build) gồm Kafka, Flink, Elasticsearch, VictoriaMetrics yêu cầu tính ổn định cực cao. Nếu hệ thống tự chạy này bị sập đúng lúc sản phẩm gặp sự cố, startup sẽ hoàn toàn "mù" thông tin. Việc đầu tư nguồn lực kỹ thuật để "chăm sóc" hạ tầng giám sát là một sự lãng phí cơ hội kinh doanh.

2. **Chi phí cơ hội của nhân sự kỹ thuật (SRE Labor Cost)**:
   - Mặc dù bảng tính toán chi phí phần cứng thuần của tự chạy (Build) luôn rẻ hơn SaaS, nhưng nó chưa phản ánh đúng **chi phí con người**.
   - Vận hành cụm Kafka + Flink + ES ở chế độ High Availability yêu cầu tối thiểu **1 đến 2 kỹ sư SRE có kinh nghiệm**. Chi phí tuyển dụng và trả lương cho SRE chất lượng cao cực kỳ đắt đỏ (khoảng $120k+/năm cho một kỹ sư tại Việt Nam/khu vực). 
   - Khi chọn SaaS (Buy), startup tốn khoảng $10k-$15k/tháng nhưng có **0% chi phí vận hành**. Đội ngũ phát triển sản phẩm hiện tại có thể tự cấu hình và sử dụng ngay lập tức mà không cần một team chuyên biệt đứng sau duy trì.

3. **Thời gian đưa vào sử dụng (Time-to-Value)**:
   - Việc thiết lập các agent Datadog chỉ mất từ **1-2 tuần** để có đầy đủ dashboard giám sát toàn hệ thống.
   - Trong khi đó, việc tự xây dựng, tối ưu hóa các pipeline lọc ghi logs, dựng cụm nén VictoriaMetrics, cấu hình Flink có thể mất từ **3 đến 6 tháng** phát triển và thử nghiệm.

### Khi nào nên chuyển từ BUY sang BUILD?
Startup chỉ nên cân nhắc xây dựng hạ tầng riêng (migration sang self-hosted) khi:
- Quy mô hệ thống tăng trưởng vượt bậc (lên mức Medium hoặc Large, số lượng hosts > 200).
- Hóa đơn SaaS hàng tháng bắt đầu tăng theo cấp số nhân (vượt ngưỡng $30,000 - $40,000/tháng).
- Sản phẩm và mô hình kinh doanh đã ổn định, và việc tối ưu chi phí hạ tầng mang lại lợi nhuận biên rõ rệt, đủ để bù đắp chi phí nuôi một đội ngũ SRE chuyên trách.
