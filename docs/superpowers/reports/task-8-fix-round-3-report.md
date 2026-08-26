# Task8 修复轮次三报告

## 研究边界

本轮只修复记忆辅助假设生成、逐条批准和正式 campaign 入口的身份绑定，不读取真实 A 股行情，不计算标签、IC、显著性、回测或机制结论。输出仍只能称为通过可见验证的候选因子；本轮测试不构成有效 Alpha、因果机制或生产结论。

## 修复内容

1. `generate-hypotheses` 生成并保存 `request_sha256`、`report_sha256`、`gap_card_sha256`、`context_sha256`、`discovery_family_id`、十个 `draft_sha256` 的 canonical `draft_batch_sha256`。`approve-hypotheses` 与 `campaign run-approved` 均要求这些字段来自 draft JSON/approved JSON 原样携带，并拒绝缺失、重写、聚合 hash 不一致和 context/family/gap 链不一致；不保存模型原文。
2. `prepare-context` 负例改为真实脱敏 policy、synthetic coverage graph、正式 ResearchMemoryStore snapshot 和 gap report 文件布局，分别覆盖 cutoff 后记忆条目与 graph/gap 链断裂；两者均在创建 context output 前非零失败。
3. 新增严格 campaign 回归：approved batch 顶层 context 被改写时，在表达式、回测和 publication 前失败，且不创建 runtime artifact。

## 验收记录

- `./.venv/bin/python -m unittest tests.test_research_evolution_cli`：26 个测试全部通过。
- 相关回归共 17 个测试：`tests.test_research_evolution` 5 个、`tests.test_research_evolution_schema` 8 个、`tests.test_research_campaign_120_slots` 1 个、`tests.test_research_campaign_runner` 2 个、`tests.test_research_campaign_evaluation` 1 个，全部通过。
- `git diff --check`：通过。

## 约束确认

保持十假设、逐条批准、9-of-10 无副作用、strict Stage C、120/设计/多样性/窗口-only/cross-arm/not_executed 约束；污染 family、无原始模型响应和无原始市场数据约束不变。未修改 Task7，未提交 `.task5-fix-round-3-tmp/`。
