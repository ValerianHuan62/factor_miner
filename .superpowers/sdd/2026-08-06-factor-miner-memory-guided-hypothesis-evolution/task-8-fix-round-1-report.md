# Task 8 修复轮次一报告

## 研究边界

本轮只修复人工十槽批准和上下文输入身份链。记忆、coverage gap 和模型草案只用于研究编排，不证明因果机制、有效 Alpha 或生产结论；真实行情、标签、IC、回测和统计评价仍只能在公司 Linux 按既有 Stage C 合同执行。

## 修复内容

- `approve-hypotheses` 先核对 draft batch 顶层 `context_sha256`、`discovery_family_id` 与 `--context`，再解析十个 draft，并由模型校验每条 draft hash、decision hash、角色和批准数量；A draft + B context/family 不能被当前 context 重建为 approved。
- `prepare-context` 只读取显式 verified graph、正式 memory index/snapshot 和显式 gap report；复用 graph loader，复核 graph manifest hash、snapshot hash、snapshot 与 graph 的绑定、gap report 与 graph/memory 的绑定，并逐条复核 cutoff、正式 family 和 entry hash。
- 增加 9/10 批准、draft/context/family 混绑、prepare 输入链失败及失败无 output 副作用的 CLI 负例测试；文档明确禁止任意 JSON/glob 身份猜测。

## 验证结果

本轮 Task 8 CLI 测试共 6 个：通过 6 个，失败 0 个。测试覆盖原有 3 个表面合同测试和新增 3 个负例/副作用测试。公司 Linux 真实候选计算、IC、回测和发布未在 Mac 执行。
