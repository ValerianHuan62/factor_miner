# ADR 0001: Standalone Project and JSONL Ledger

- Status: Accepted
- Date: 2026-07-15

## Context

因子发现引擎未来需要脱离 `huan_quant` 独立运行。把核心放进 `huan_quant` 会把研究引擎绑定到个人 Skill、项目内部目录和 PostgreSQL 实例；而 V0 只有单用户、单机、顺序执行需求。

## Decision

1. 建立独立 `factor_miner` Git 仓库；核心不 import `huan_quant`。
2. `huan_quant` 未来只是人工审查后的可选消费者，不属于 V0。
3. V0 使用 immutable canonical JSON 保存 candidate/campaign，使用 append-only hash-chained JSONL 保存 TrialEvent。
4. JSONL 是 V0 主存储，不是 PostgreSQL 不可用时的 fallback。
5. V0 强制单 writer；不实现并行任务、共享服务或 sealed evaluator。

## Consequences

优点：依赖少、可直接审计、易于重放、适合先验证研究主链路。代价：查询能力、事务边界、多用户授权和并发能力有限。

出现以下任一条件时必须设计 PostgreSQL 迁移，而不是继续扩展 JSONL：

- 两个或更多并发 writer
- 多用户共享修改或审批
- sealed OOS 需要身份与权限隔离
- 任务队列、远程 worker 或服务化 API
- ledger 查询、索引或恢复成本明显影响研究

迁移时沿用同一 Candidate/Campaign/TrialEvent 模型；不得改变已登记 ID、hash 或事件语义。
