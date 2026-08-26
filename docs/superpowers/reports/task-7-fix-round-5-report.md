# Task 7 第五轮修复报告

## 研究边界

本轮只修复正式研究记忆的文件事务语义与 Dashboard typed 投影，不读取真实 A 股行情，不计算真实 IC、冗余或回测。测试使用程序生成的合成条目；通过结果仍只能说明工程链路具备可见验证条件，不能说明因子有效、Alpha 或机制成立。

## 修复内容

1. `publish_campaign_memory` 现在先完成 120 个 `ResearchMemoryEntry` 的类型和身份预验证，再在同一 writer lock 内追加对象、事件和 index，并写入 snapshot/reference。任何 append、index、snapshot 或 reference 异常都会恢复调用前的 entries、对象、index、snapshot 和 reference 文件集合；相同 typed entry 的重放保持幂等。
2. Dashboard `research_memory_entries` 投影改用 schema 的五类结构签名和 gap labels，`summary_json` 只保存脱敏 `evaluation_summary` 与 `data_identity_summary`，manifest hash 来自 `data_identity_summary.manifest_hash`，创建时间来自 `created_at`，并保留 coverage graph、snapshot、entry hash、终态、失败和重复身份。
3. 保持 import boundary、strict self-evolution、无 raw model/market、污染 family 隔离和 120 slots 合同。

## 验证

- 相关回归：33 个测试通过。
- 全量 `unittest discover`：502 个测试通过。
- `git diff --check`：通过。

结论仍限于“通过可见验证的候选因子”所需的工程闭环，不提供金融研究结论。
