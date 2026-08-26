# Task 7 修复 Round 2 完成报告

## 研究边界

本轮只修复 Dashboard 研究记忆 typed governance 投影和演化批次重放身份校验，不执行真实 A 股计算、统计评价、机制检验或生产结论判断。正式研究记忆仍只保存脱敏槽位状态、离散摘要和内容身份；原始模型响应、A 股数据和密钥未写入代码或测试。

## 修复内容

- `research_evolution_batches` 增加 `approval_count`，`research_gap_summaries` 增加 `truncated_count`，`research_memory_snapshots` 增加 `terminal_counts`；migration 006 同时包含初始定义和 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，旧安装和无演化 metadata 的旧调用保持兼容。
- PostgreSQL 投影完整写入三项治理字段，并在已有 typed 行上进行幂等内容校验；Dashboard 快照仍保留相同字段，读取时不会只依赖不可见的 JSON reference。
- 同一 `context_id` 重放时逐字段比较 context、family、approval、coverage graph、memory、gap、policy、预算、approval 数量和 source manifest 身份；任何差异均抛出 `LEDGER_CORRUPT`，只有完全相同才保留 `ON CONFLICT DO NOTHING` 的幂等语义。
- 增加 fake connection 测试，覆盖三项治理字段的 INSERT 参数可见性以及独立 `approval_batch_hash` 冲突。

## 验证

- `UV_CACHE_DIR=/tmp/factor_miner_uv_cache uv run python -m unittest tests.test_research_memory_projection tests.test_dashboard_projection tests.test_dashboard_contract tests.test_research_campaign_evaluation -v`：16 项通过。
- `UV_CACHE_DIR=/tmp/factor_miner_uv_cache uv run python -m unittest tests.test_research_memory tests.test_research_campaign_runner tests.test_research_campaign_120_slots tests.test_research_gap -v`：27 项通过。
- `git diff --check`：通过。

## 风险边界

这些测试使用合成数据和 fake connection，没有连接真实 PostgreSQL，也没有执行公司 Linux 的真实候选计算或统计评价。此次改动不触及 Task 8 或 CLI，不提交 `.task5-fix-round-3-tmp/`。
