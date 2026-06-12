# Engine Khắc Phục Sự Cố Dựa Trên Bằng Chứng

Một bộ gợi ý khắc phục sự cố có thể giải thích, dựa trên bằng chứng. Nó nhận một sự cố
có cấu trúc (logs + traces + metrics + topology), so sánh với kho sự cố lịch sử, rồi
đề xuất một hành động kèm độ tin cậy đã hiệu chỉnh và một chuỗi audit đầy đủ — hoặc leo
thang (`page_oncall`) khi đầu vào là lạ hoặc rủi ro.

Luồng xử lý: **incident JSON → trích đặc trưng (`features.py`) → truy hồi tương đồng +
bỏ phiếu theo kết quả (`retrieval.py`) → quyết định theo chi phí/blast-radius
(`decision.py`) → JSON quyết định + `audit.jsonl`**.

## Cài đặt

Cần Python 3.11+ và `pyyaml` (phụ thuộc bắt buộc duy nhất; `numpy`/`matplotlib`/
`scikit-learn` là tuỳ chọn, chỉ dùng cho notebook phân tích và xuất ảnh).

```bash
pip install pyyaml
# tuỳ chọn, cho analysis.ipynb và make_plots.py:
pip install numpy matplotlib jupyter nbconvert
```

> Trên Windows nếu lệnh `python` không có, dùng `py` thay thế (vd: `py engine.py ...`).

## Chạy một sự cố

```bash
python engine.py decide --incident eval/E01.json \
                        --history incidents_history.json \
                        --actions actions.yaml
```

Lệnh này in JSON quyết định ra stdout và **ghi thêm một dòng** vào `audit.jsonl`.
Kết quả mong đợi cho E01: `selected_action = rollback_service`, `params.service =
payment-svc`, kèm khối `evidence` liệt kê các lân cận hàng đầu, phiếu ứng viên theo
kết quả, và các tín hiệu đã dùng.

## Chạy cả 8 sự cố eval

PowerShell (Windows):

```powershell
Remove-Item audit.jsonl -ErrorAction SilentlyContinue
foreach ($i in '01','02','03','04','05','06','07','08') {
  py engine.py decide --incident "eval/E$i.json" --history incidents_history.json --actions actions.yaml | Out-Null
}
```

bash:

```bash
rm -f audit.jsonl
for i in 01 02 03 04 05 06 07 08; do
  python engine.py decide --incident eval/E$i.json \
                          --history incidents_history.json \
                          --actions actions.yaml >/dev/null
done
```

> Lưu ý: engine *ghi thêm* vào `audit.jsonl`, nên hãy xoá file trước khi chạy lại sạch
> để tái lập đúng 8 dòng.

## Chấm điểm

```bash
python grade.py --audit audit.jsonl --expected eval/expected.json
```

Tái lập: **Correct 8/8, Forbidden 0/8, Missing 0/8**, auto-rubric 85/85.

## Xuất biểu đồ (ảnh PNG)

```bash
py make_plots.py
```

Sinh ra 6 ảnh trong thư mục `plots/` (được chèn vào `FINDINGS.md`). Notebook
`analysis.ipynb` cũng nhúng các biểu đồ tương tự kèm giải thích bên dưới mỗi hình.

## Các file

| File | Vai trò |
|---|---|
| `engine.py` | Điểm vào CLI, ghép 3 layer, ghi chuỗi audit |
| `features.py` | Layer 1 — chuẩn hoá log, trích trace/metric, suy ra service bị ảnh hưởng có củng cố |
| `retrieval.py` | Layer 2 — tương đồng fused hybrid + bỏ phiếu theo kết quả + cờ OOD |
| `decision.py` | Layer 3 — utility theo cost/blast/downtime + các cổng leo thang |
| `audit.jsonl` | một quyết định cho mỗi sự cố eval, kèm khối `evidence` đầy đủ |
| `FINDINGS.md` | 5 câu trả lời phản tư bắt buộc (kèm số liệu thật) + tuỳ chọn A/B/C |
| `make_plots.py` | xuất 6 biểu đồ ra `plots/*.png` |
| `analysis.ipynb` | notebook biểu đồ giải thích quyết định |
| `plots/` | các ảnh PNG đã xuất |

`actions.yaml` giữ nguyên so với catalog gốc.
