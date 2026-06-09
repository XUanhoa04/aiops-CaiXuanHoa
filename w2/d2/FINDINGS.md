# FINDINGS.md — W2/D2 Root Cause Analysis

## Phân tích Cluster Chính
- **Root Cause**: `payment-svc`
- **Lý do**: Khi chạy thuật toán Graph Traversal kết hợp với Timestamp (Temporal), `payment-svc` có điểm cao nhất (khoảng 0.82) vì nó nằm ở vị trí sâu nhất trong cascade chain đang bị alert (không gọi service nào khác trong cluster đang alert) và các lỗi bắt nguồn từ nó lan truyền sang `checkout-svc` và `edge-lb`. Đồng thời, retrieval engine kNN cũng match chính xác sự kiện này với incident lịch sử `INC-2025-11-08` (pool exhaustion) dựa trên sự tương đồng về service và severity.

## Confidence & Auto-remediation
- **Confidence Output**: 0.82 (hoặc tương đương tùy phân phối của graph+retrieval score).
- **Có dám deploy auto-remediation (rollback) không?**: Với mức tự tin 0.82 dựa trên kNN retrieval từ 30 incident lịch sử, em chưa dám bật auto-remediation (đặc biệt là auto-rollback) trực tiếp trên production nếu không có sự xác nhận của SRE. Rollback là một hành động có risk (có thể làm mất data hoặc gián đoạn transaction), do đó auto-rollback chỉ nên được cấu hình khi confidence > 0.90 hoặc khi metric `connection_pool_used_ratio` chạm nóc 100% trong 60s liên tục (như lịch sử incident đã chỉ ra).

## 1 Case không chắc chắn
- **Case `recommender-svc`**: Trong cluster chính có sự xuất hiện của `recommender-svc` do quá trình topology grouping (max_hop=2) của W2/D1 vô tình gom vào. Tuy nhiên, nó bị dính một cảnh báo về `cpu_utilization|warn` vốn chỉ là một concurrent batch retrain, không liên quan đến chuỗi cascade của payment-svc. Pipeline RCA hiện tại có thể bị "nhiễu" một chút ở phần overlap score do dịch vụ này, khiến sự chính xác của việc khoanh vùng sự cố không đạt 100%.

## Bonus Path
- **Lựa chọn**: Em chọn thực hiện **Bonus 1 — Decision Tree**.
- **Tiến hành**: Em đã huấn luyện một mô hình `DecisionTreeClassifier` trên tập dữ liệu 30 incident, sử dụng các feature gồm: `services_set` (one-hot encoding), `severity_max`, và `time_burst_pattern` (sử dụng `mttd_min` làm proxy). Sau đó, chia tập dữ liệu train/test (70/30) và so sánh độ chính xác của Decision Tree với phương pháp lấy top-1 kNN Retrieval hiện tại.
- **Kết quả & Phân tích**:
  - Accuracy của Decision Tree trên Test Set: **0.00%**
  - Accuracy của kNN Heuristic trên Test Set: **11.11%**
  - **Lý giải**: Cả hai phương pháp đều cho kết quả độ chính xác (accuracy) rất thấp vì tập dữ liệu quá nhỏ (30 samples) và nhãn `root_cause_class` hầu như rất ít lặp lại (mỗi nhãn thường chỉ xuất hiện 1-2 lần). Khi tách test set, mô hình Decision Tree không có khả năng dự đoán đúng một class chưa từng gặp trong tập train. Ngược lại, kNN có độ chính xác nhỉnh hơn một chút nhờ cơ chế tính điểm heuristic linh hoạt dựa trên sự liên quan của domain (keyword, overlapping services). Do đó, đối với tập dữ liệu sparse và nhỏ gọn như thế này, **retrieval-based kNN vẫn vượt trội và phù hợp hơn rất nhiều so với việc cố gắng huấn luyện một mô hình Machine Learning cổ điển**.
