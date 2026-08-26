# Task8 修复轮次四报告

## 研究边界

本轮只修复记忆辅助假设批准 wrapper 的身份核验和 `prepare-context` 的脱敏输入负例，不读取真实 A 股行情，不计算标签、RankIC、显著性、回测或机制结论。结果仍只能称为通过可见验证的候选因子，不构成有效 Alpha、因果机制、生产结论或实盘建议。

## 修复内容

1. `campaign run-approved` 新增专用批准批次原始身份校验。它只从 approved wrapper 读取 `context_sha256`、`discovery_family_id`、`approvals`，排除 `approval_batch_sha256` 后按 `ApprovedEvolutionHypothesisBatch` schema 的 canonical 内容重算，并与原始 hash 比较。逐条 `decision_sha256`、draft provenance、H01-H10 十槽覆盖、批准角色和 family 约束仍然保留。
2. 新增 `approve-hypotheses` 原样输出到 `campaign run-approved` 的合成 round-trip 回归。严格入口通过 wrapper 批次身份闸门后，才在后续缺失依赖处失败；不进入 expression、backtest 或 publication，也不创建 runtime artifact。
3. `prepare-context` 使用合法 policy、真实 `ResearchMemoryStore` loader、synthetic coverage graph、snapshot 和 gap report，补充 `published_at > cutoff`、terminal state 不在 `include_terminal_states`、snapshot family 超出白名单三类具体负例；每类均断言非零、具体错误和 output 不存在。测试未 mock 整段 `prepare-context` 流程。

## 验收记录

- `./.venv/bin/python -m unittest tests.test_research_evolution_cli`：30 个测试全部通过。
- 相关回归命令：`tests.test_research_evolution_cli tests.test_llm_run_approved_cli tests.test_llm_hypothesis tests.test_research_evolution tests.test_research_evolution_schema tests.test_research_campaign_120_slots tests.test_research_campaign_runner tests.test_research_campaign_evaluation`：58 个测试全部通过。
- `git diff --check`：通过。

## 约束确认

保持 9-of-10 无副作用、strict Stage C、120 槽、设计多样性、window-only、cross-arm、`not_executed` 和 mismatch 在 expression/backtest/publication 前失败；未放松逐条 hash、draft provenance、十槽或角色约束。未引入原始模型响应、原始市场数据或真实 campaign/A 股执行。未修改 Task7，未提交 `.task5-fix-round-3-tmp/`。
