# Task5 修复轮次 3 报告

本轮修复只绑定外发请求的公开提示内容与审计哈希，不改变因子研究、评价或生产结论。候选因子仍只能称为通过可见验证的候选因子，不能据此推断有效 Alpha 或实盘收益。

- 当 `export_payload` 含 `public_payload_sha256` 时，解析 `body['messages'][1]['content']` 的 canonical JSON，并要求其 SHA-256 等于审计中的公开 payload hash。
- `PreparedDeepSeekRequest` 的模型校验与 `verify_evolution_export_authorization` 均执行该绑定检查；缺少该字段的 legacy request 保持兼容。
- 增加伪造公开内容但沿用原审计 hash 的失败覆盖。

验证命令：`PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_llm_online tests.test_research_evolution`。
