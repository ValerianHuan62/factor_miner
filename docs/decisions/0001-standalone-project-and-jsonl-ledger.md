# 架构决策 0001：独立项目与 JSONL 账本

- 状态：已采纳
- 日期：2026-07-15

## 背景

因子发现引擎需要脱离 `huan_quant` 独立运行。把核心放进 `huan_quant` 会把研究引擎绑定到个人 Skill、项目内部目录和 PostgreSQL 实例；当前写入模型只需要单用户、单机和顺序执行。

## 决策

1. 建立独立 `factor_miner` Git 仓库；核心不 import `huan_quant`。
2. `huan_quant` 只是人工审查后的可选消费者，不属于本项目。
3. 使用 immutable canonical JSON 保存 candidate/campaign，使用 append-only hash-chained JSONL 保存 TrialEvent。
4. JSONL 是主存储，不是 PostgreSQL 不可用时的 fallback。
5. 正式账本强制单 writer；并发研究通过上层队列串行提交。

## 影响

优点：依赖少、可直接审计、易于重放、适合先验证研究主链路。代价：查询能力、事务边界、多用户授权和并发能力有限。

出现以下任一条件时必须重新设计写入模型，而不是继续扩展单写入 JSONL：

- 两个或更多并发 writer
- 多用户共享修改或审批
- sealed OOS 需要身份与权限隔离
- 多写入器任务队列或服务化 API
- ledger 查询、索引或恢复成本明显影响研究

迁移时沿用同一 Candidate/Campaign/TrialEvent 模型；不得改变已登记 ID、hash 或事件语义。
