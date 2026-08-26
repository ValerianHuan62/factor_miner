# Task8 修复轮次二报告

## 研究边界

本轮只强化记忆辅助假设演化的审计输入闸门，不计算行情、标签、IC、回测或机制结论。输出仍只能称为通过可见验证的候选因子；任何 hash、截止日或终态不一致都不能被解释为研究结果。

## 修复内容

1. `prepare-context` 在正式 memory store 校验后，逐条复核 snapshot、index、entries 账本、entry 对象和已发布运行 input manifest。每条入选 entry 必须满足 `created_at <= cutoff_at`、`published_at <= cutoff_at`、终态属于冻结 `include_terminal_states`、family 属于冻结白名单，并与 coverage graph、snapshot 和 gap report 身份链一致。
2. CLI 在 Pydantic 解析前检查 context、report、gap card、draft、decision 和 approved batch 的显式 hash；缺字段、空字段或重算不一致均非零退出。生成产物保存 report 与 gap card 审计字段。
3. CLI 负例覆盖缺失 draft/decision/report/gap card hash，并确保失败不创建 output；测试数据仅使用脱敏合成内容。

## 验收记录

- `tests.test_research_evolution_cli`：8 个测试全部通过。
- `git diff --check`：通过。
- 相关回归：Task8 验收命令及回归命令的实际结果见提交前终端记录；本报告不宣称真实服务器数据或统计结果通过。

## 约束确认

保持 A/B/C、未来信息防火墙和 `campaign run-approved` strict Stage C 约束；未修改 Task7，未提交 `.task5-fix-round-3-tmp/`，未写入真实行情、模型原文、密钥或公司数据。
