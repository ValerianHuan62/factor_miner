# 从这里开始

本文件用于让新 Codex 对话在没有旧聊天记忆的情况下安全继续。

## 固定启动提示词

```text
这是独立 factor_miner 项目。请先完整读取 AGENTS.md、
skills/quant_coding/SKILL.md、
docs/V0.1金融流程说明.md、
docs/V0.2增量信息金融流程说明.md、
docs/V0.3市场状态金融流程说明.md、
docs/V0.4因子覆盖图谱金融流程说明.md、
docs/superpowers/specs/2026-07-15-factor-miner-v0-design.md、
docs/superpowers/specs/2026-07-17-factor-miner-v0.1-trusted-visible-design.md、
docs/superpowers/specs/2026-07-27-factor-miner-v0.2-incremental-information-design.md、
docs/constraints/RESEARCH_GOVERNANCE.md、
docs/constraints/FACTOR_RESEARCH_PROTOCOL.md、
docs/contracts/company-a-share-data.md 和
docs/superpowers/plans/2026-07-15-factor-miner-v0.md。
然后读取 docs/superpowers/plans/2026-07-17-factor-miner-v0.1-trusted-visible.md。
最后读取 docs/superpowers/plans/2026-07-27-factor-miner-v0.2-incremental-information.md。
若开始 V0.3，再读取
docs/superpowers/specs/2026-07-29-factor-miner-v0.3-walk-forward-hmm-market-regime-design.md 和
docs/superpowers/plans/2026-07-28-factor-miner-v0.3-v0.5-regime-coverage-and-llm.md。
若执行 V0.3，再读取
docs/superpowers/plans/2026-07-29-factor-miner-v0.3-walk-forward-hmm-market-regime.md。
若检查或继续 V0.4，再读取
docs/superpowers/specs/2026-07-29-factor-miner-v0.4-dual-layer-coverage-graph-design.md 和
docs/superpowers/plans/2026-07-29-factor-miner-v0.4-dual-layer-coverage-graph.md。
然后检查 git status 和最近提交，从实施计划中第一个未完成任务继续。
真实数据计算只允许在公司服务器执行；V0.2 只产生通过可见增量信息验证的候选因子，
V0.3 只产生市场状态日历，不接在线大语言模型、不使用 PostgreSQL、不做密封样本外检验，
也不导入 huan_quant。
```

## 事实源优先级

发生冲突时按以下顺序处理：

1. 当前用户明确指令
2. `AGENTS.md` 硬规则
3. 已批准技术设计
4. 实施计划
5. 数据合同与架构决策记录
6. Git 历史
7. 聊天摘要或个人记忆

## 恢复步骤

1. 运行 `git status --short`，保护任何已有未提交修改。
2. 运行 `git log -5 --oneline --decorate`，确认最近完成边界。
3. V0.1、V0.2、V0.3 与 V0.4 已完成本地和公司 Linux 服务器工程验收；V0.2 真实样例终态为 `incremental_failed`，不能作为通过候选。
4. V0.3 正式状态快照 ID 为 `regsnap_12b8bd1c99790513d1dd4941`；它只证明市场状态流程和经济区分度，不证明下游交易价值。
5. V0.4 正式覆盖图谱 ID 为 `covgraph_3e3b6821f534dad40b7369d7`；它只描述可见区间的结构和信号覆盖，不是因子认证或生产结论。
6. V0.5 尚未实施；开始编码前必须先批准并冻结 LLM 脱敏导出、权限、提示、模型身份和研究预算。
7. 只完成该任务；运行任务写明的测试与静态检查。
8. 更新计划复选框，并用单独提交保存。
9. 真实数据步骤只能在公司服务器执行；服务器不可达时保持“本地完成、服务器待验收”，不得伪造完成状态。

不要复制旧聊天作为事实源，也不要因为缺少聊天记忆而重新设计已批准的边界。
