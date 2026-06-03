# Báo cáo Bài tập W1-D3: Data Layer Architecture & Cost Model

Bài viết tổng hợp thiết kế kiến trúc E2E, bảng tính toán chi phí (cost model) và quyết định kiến trúc (ADR) cho Payment Service.

---

## 1. Sơ đồ Kiến trúc Data Layer E2E

Chi tiết kiến trúc và luồng dữ liệu (Metrics, Logs, Traces) của Payment Service được thiết kế tại [architecture.md](./architecture.md).

Sơ đồ tổng thể (LR):
![E2E Payment Service Telemetry Architecture Diagram](architecture.png)

---

## 2. Kết quả Đánh giá Chi phí (Cost Estimation)

Bảng so sánh chi phí hàng tháng được tính toán từ script [cost_model.py](./cost_model.py):

### Bảng tổng hợp chi phí (USD/tháng)
| Quy mô (Tier) | Tự chạy (Build) | Datadog SaaS (Buy) | Tiết kiệm / tháng | Đề xuất lựa chọn |
| :--- | :---: | :---: | :---: | :--- |
| **Small** (10 services, 15 hosts) | $2,829.81 | $4,360.00 | $1,530.19 | **Buy (Datadog)** (Xem Reflection) |
| **Medium** (100 services, 150 hosts) | $16,308.07 | $41,100.00 | $24,791.93 | **Build (Self-Hosted)** |
| **Large** (1000 services, 1500 hosts) | $87,220.66 | $411,000.00 | $323,779.34 | **Build (Self-Hosted)** |

*Ghi chú về chi phí Build:* Đã cộng gộp chi phí hạ tầng (VM compute, EBS GP3/S3 storage, network replication) và chi phí nhân sự vận hành (SRE labor: 0.2 FTE cho Small, 1 FTE cho Medium, 3 FTE cho Large, tính baseline $10k/month/SRE).

---

## 3. Tóm tắt Quyết định Kiến trúc (ADR Summary)

*   **Vấn đề**: Log của Payment Service rất nhiều (500GB/ngày ở tier Medium), nếu đẩy 100% vào Elasticsearch để lưu 30 ngày sẽ cực kỳ tốn RAM/Disk.
*   **Giải pháp (Loki vs ES)**: Dùng **Tiered Storage**.
    *   **Hot logs (0-7 ngày)**: Lưu Elasticsearch để devs search nhanh bằng full-text index.
    *   **Warm logs (7-30 ngày)**: Đẩy qua Grafana Loki lưu trên S3 (chỉ index label metadata) giúp giảm 75% chi phí lưu trữ logs thô.
    *   Chi tiết xem tại [ADR-001.md](./ADR-001.md).

---

## 4. Ý kiến cá nhân (SRE Reflection)

**Câu hỏi**: *Nếu được thuê làm Platform Engineer cho startup 50 services mới gọi vốn Series A, bạn khuyên nên BUILD hay BUY?*

**Đề xuất của mình: BUY (Dùng SaaS như Datadog)**

**Lý do:**
1.  **Focus vào Core Product**: Startup Series A cần chạy đua tìm Product-Market Fit (PMF). Tự build cụm Kafka + Flink + ES/Loki rất mất thời gian. Gặp outage giám sát đúng lúc app lỗi là coi như đi đứt.
2.  **Chi phí cơ hội của kỹ sư**: Nhìn bảng tính trên thì Build rẻ hơn, nhưng đó là khi SRE chỉ tốn 0.2 FTE ($2,000). Thực tế, để vận hành đống Kafka/Flink HA cần ít nhất 1-2 SRE cứng ($120k+/năm mỗi người), đắt hơn nhiều so với bill Datadog lúc này.
3.  **Time-to-Value**: Cắm agent Datadog mất 1 tuần là xong. Tự build, tối ưu, test tải mất ít nhất 3-6 tháng.

**Khi nào thì migrate sang BUILD?**
Chỉ chuyển sang tự build khi scale tăng mạnh (hosts > 200), bill Datadog vượt ngưỡng $30k-$40k/tháng, sản phẩm đã ổn định và công ty bắt buộc phải tối ưu hóa biên lợi nhuận. Lúc đó việc tuyển team SRE riêng mới thực sự kinh tế.
