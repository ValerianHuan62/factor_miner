# Task 7 修复轮次 3 报告

## 研究边界

本轮只修复 PostgreSQL Dashboard read model 的投影参数顺序与旧库升级语义，不改变因子计算、评价协议、候选主张、市场数据或 CLI。候选结果仍只能解释为通过可见验证的候选因子，不能据此宣称有效 Alpha 或生产结论。

## 修复内容

- 提交：`2ae7e451595dd15a0012fdc5df9689b064156c70`（`fix: preserve legacy governance projections`）。
- `dashboard/pg_store.py`：修正 `research_gap_summaries` INSERT 的十列十参数位置映射；补充旧行 NULL legacy marker 的严格比较与安全回填。已有非 NULL 的 `approval_count`、`truncated_count`、`terminal_counts` 仍必须与重放值一致，否则硬失败。
- `dashboard/migrations/006_research_evolution_memory.sql`：ALTER 路径新增字段保留 NULL 作为 legacy marker；新建表保持非负与 JSON object 约束；旧表通过可重复执行的约束块补齐相同的不变量，不覆盖已有真实值。
- `tests/test_research_memory_projection.py`：按位置断言 gap 的全部参数，增加 legacy nullable 回填/不覆盖 fake connection 测试，以及迁移约束/schema 合同测试。

## 不变量核对

- memory snapshot 父表 INSERT 仍先于 entries child，FK 仍为 `NOT DEFERRABLE`。
- 四个投影表的 identity、approval、terminal、truncated 字段仍完整投影；相同内容幂等，hash/identity 冲突硬失败。
- 未修改 Task 8、CLI、污染 family 路径，也未引入 broad except、原始行情或模型原文。
- `.task5-fix-round-3-tmp/` 未暂存、未提交。

## 测试结果

- `env UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest tests.test_research_memory_projection tests.test_research_gap tests.test_dashboard_projection tests.test_dashboard_contract tests.test_research_campaign_evaluation -v`：27 项通过。
- 迁移约束补充后再次运行 `env UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest tests.test_research_memory_projection -v`：9 项通过。
- `git diff --check`：通过。
