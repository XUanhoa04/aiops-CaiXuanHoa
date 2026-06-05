# Detection Approach — DESIGN.md

## Approach em dùng
**Phương pháp Lai giữa Ngưỡng Cố định An toàn (Safe Absolute Thresholds) và Giám sát Từ khóa Log (Log Keyword Monitoring) có Trạng thái (Stateful).**

## Tại sao chọn approach này
- **Phù hợp hoàn hảo cho dữ liệu Streaming**: Dữ liệu được xử lý tức thời theo từng tick (POST request) mà không yêu cầu lưu trữ và tính toán lại toàn bộ lịch sử dài hạn phức tạp, giúp giảm thiểu độ trễ xử lý (low overhead) và phát hiện lỗi cực kỳ nhanh chóng.
- **Độ chính xác tuyệt đối (0% False Alarms)**: Khoảng cách giữa hành vi bình thường và hành vi bất thường trong lab này được định nghĩa rất lớn (ví dụ: memory bình thường ~800MB, khi lỗi tăng đến 2GB; rps bình thường tối đa ~160, khi lỗi tăng lên >220). Việc sử dụng ngưỡng tuyệt đối nằm ngoài biên phân phối chuẩn (hơn 7-sigma) đảm bảo không bao giờ bị kích hoạt nhầm bởi nhiễu thông thường.
- **Thời gian phát hiện nhanh (TTD cực thấp)**: Kích hoạt alert ngay lập tức sau 2 ticks liên tiếp phát hiện bất thường mà không cần đợi chạy các mô hình học máy nặng nề.

## Cách hoạt động
1. **Trích xuất Đặc trưng (Feature Extraction)**: Với mỗi payload gửi đến, pipeline trích xuất các thông số metrics chính (`memory_usage_bytes`, `upstream_timeout_rate`, `http_requests_per_sec`) và duyệt qua danh sách các logs đi kèm.
2. **Kiểm tra Ngưỡng Metrics**:
   - `memory_usage_bytes > 950,000,000` -> Nghi ngờ `memory_leak`.
   - `upstream_timeout_rate > 1.5%` -> Nghi ngờ `dependency_timeout`.
   - `http_requests_per_sec > 220` (và `upstream_timeout_rate < 0.8%`) -> Nghi ngờ `traffic_spike`.
3. **Phân tích Log hỗ trợ**: Scan các log messages để tìm từ khóa đặc trưng (ví dụ: "OutOfMemory", "Queue depth high", "Circuit breaker OPEN"). Nếu có, xác nhận ngay lập tức loại lỗi tương ứng.
4. **Kiểm soát Trạng thái (Stateful Verification)**: Để lọc nhiễu ngẫu nhiên, pipeline yêu cầu chỉ số bất thường duy trì trong ít nhất **2 ticks liên tiếp** trước khi ghi nhận alert vào `alerts.jsonl`. Sau khi ghi nhận alert cho một sự cố, các cảnh báo trùng lặp sẽ bị chặn để tránh spam file.
5. **Cơ chế Tự động Reset**: Nếu nhận thấy timestamp đi lùi (generator khởi động lại) hoặc các chỉ số quay lại mức bình thường trong nhiều ticks liên tiếp, hệ thống sẽ reset trạng thái sẵn sàng cho đợt kiểm thử tiếp theo.

## Parameters em chọn
- **Memory limit/threshold**: Ngưỡng `950,000,000 bytes` (~950MB). Lý do: Memory bình thường phân phối quanh mức ~800MB (std 20MB). Giá trị 950MB cách xa mean hơn 7.5 lần standard deviation (7.5-sigma), triệt tiêu hoàn toàn khả năng false alarm nhưng vẫn đảm bảo phát hiện rò rỉ bộ nhớ ở giai đoạn rất sớm (ngay khi progress rò rỉ đạt ~15%).
- **Timeout threshold**: Ngưỡng `1.5%`. Lý do: Bình thường upstream timeout tối đa chỉ ~0.4%. Ngưỡng 1.5% giúp phân biệt hoàn hảo lỗi dependency với nhiễu mạng bình thường chỉ trong tick đầu tiên.
- **RPS threshold**: Ngưỡng `220`. Lý do: Traffic bình thường cao nhất khoảng 160 rps + nhiễu cực đại (~200 rps). Ngưỡng 220 đảm bảo an toàn tuyệt đối.
- **Consecutive Anomalous Ticks**: `2 ticks`. Giúp loại bỏ hoàn toàn các điểm đột biến (spike) tức thời ngẫu nhiên do nhiễu Gaussian mà không ảnh hưởng đáng kể đến MTTR (chỉ mất thêm 1 tick phát hiện).

## Cải thiện nếu có thêm thời gian
1. **Dynamic Thresholds (Adaptive Limits)**: Nếu hệ thống có sự thay đổi tải theo chu kỳ ngày/đêm lớn hơn hoặc cấu hình hạ tầng thay đổi, ta có thể tích hợp **STL Decomposition** hoặc **EWMA** (Exponentially Weighted Moving Average) để tính toán ngưỡng động (dynamic threshold) tự điều chỉnh theo chu kỳ thời gian thực thay vì dùng ngưỡng tĩnh.
2. **Drain3 Log Parsing**: Tích hợp Drain3 để tự động gom nhóm log và phát hiện các template mới (New Log Template) mà không cần hardcode từ khóa. Điều này giúp hệ thống phát hiện các lỗi chưa biết trước (Unknown Faults).
