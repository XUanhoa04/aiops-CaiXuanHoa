# SUBMIT.md — W2/D2 EOD Checkpoint

### 1. Confidence của top-1 trong cluster lớn nhất bạn xử lý là bao nhiêu? Nếu phải set threshold để auto-rollback (không cần SRE confirm), bạn pick số nào? Lý do?
- **Confidence thực tế:** Confidence cho `payment-svc` làm root-cause (class: `connection_pool_exhaustion`) sau khi tính điểm combined (graph + kNN retrieval) đạt khoảng 0.82.
- **Threshold auto-rollback đề xuất:** Em sẽ set ngưỡng là **0.90**.
- **Lý do:** Rollback code là một hành động rủi ro cao (ảnh hưởng user requests, có khả năng gãy data state). Chỉ nên auto-remediate khi hệ thống chắc chắn >90% rằng lỗi thuộc nhóm code deploy (bad_deploy hoặc connection_pool_exhaustion ngay sau khi deploy) VÀ đã match trực tiếp với một lịch sử sự cố (incident) rõ ràng. Với 0.82, SRE vẫn nên review để tránh "chữa lợn lành thành lợn què".

### 2. Variant bạn chọn cho classifier (A rule-based / B free LLM / C paid LLM). Chạy thực tế ra sao? Trade-off với variant bạn không chọn?
- **Variant chọn:** **A (Rule-based / Retrieval kNN-style)**.
- **Chạy thực tế:** Chạy cực nhanh (<0.1s), deterministic (luôn ra 1 kết quả giống nhau), và code dễ debug. Output JSON validate dễ dàng không lo format rác.
- **Trade-off:** Nó hoàn toàn phụ thuộc vào việc sự kiện đã có trong tập `incidents_history.json` hay chưa. Nếu một service mới gây ra một kiểu lỗi mới (zero-day incident), hệ thống sẽ fallback về "other" hoặc map sai. Nếu chọn LLM (B/C), khả năng suy luận trên sự kiện mới tốt hơn, nhưng trade-off lớn là độ trễ (có thể vài giây), tốn chi phí (paid LLM), và đặc biệt là rủi ro bị "hallucination" khi LLM đoán bừa root cause ở một service không hề alert.

### 3. Đọc bảng Industry landscape (§6) — pipeline bạn xây gần product nào nhất? Trong domain GeekShop (e-commerce, alert volume cao, service map tương đối ổn định), lựa chọn đó hợp lý hay nên đổi?
- **Gần Product nào nhất:** Pipeline này (gom alert thành cluster dựa vào graph, sau đó ranking bằng PageRank/Graph Traversal + Retrieval) rất gần với triết lý của **Dynatrace Davis** (Smartscape-based topology traversal) kết hợp với các system match incident lịch sử như Moogsoft.
- **Tính hợp lý cho GeekShop:** Lựa chọn này là **rất hợp lý** vì GeekShop là domain e-commerce có kiến trúc microservices và "service map tương đối ổn định". Khi topology ít bị thay đổi xoành xoạch như serverless/FaaS, dùng Service Graph (Dependency Graph) làm trụ cột cho RCA sẽ đem lại kết quả cực kỳ deterministic và nhanh chóng. Nếu kiến trúc chuyển sang event-driven/async cực mạnh, ta mới cân nhắc đổi sang Causely (học Causal graph từ data).
