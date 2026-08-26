# 从 V0 迁移到 V0.1

V0.1 不是对旧批次的原地升级。它新增了类型化交易时点、代码冻结的评价政策、跨批次研究族预算、输入与结果物理隔离、未来依赖探针、强制参考因子检查和原子产物发布，因此旧候选与旧批次的内容发生了变化，内容寻址 ID 也必须变化。

## 历史记录怎么处理

第一次 V0 可见运行必须原样保留，标记为 `legacy_untrusted_visible`。不得删除或改写它的候选、批次、账本和产物，也不得把它加入 V0.1 参考池。它只能证明“当时运行过什么”，不能成为 V0.1 候选结论。

V0.1 建议使用新的 artifact root，避免旧版恢复逻辑误解历史事件。若必须共用上级目录，至少应让 V0 与 V0.1 使用互不重叠的 `state` 和 `artifacts` 根目录。

## 迁移步骤

1. 将旧候选人工转换为 `TrustedCandidateFactorSpec`：`spec_version` 改为 `"2"`，把自由文本可得性替换为 `close_t → after_close_t → open_t_plus_1`，重新核对事前假设和来源。不得根据旧结果修改假设。
2. 用 `validate-spec` 和 `compile-spec` 先得到新候选 ID。每个参数变体和历史失败尝试都要占用独立 hypothesis slot。
3. 准备代码解析政策对应的 `EvaluationPolicySpec`、覆盖全部候选的新 `ResearchFamilySpec`、`TrustedVisibleCampaignSpec` 和冻结参考因子清单。
4. 在结果数据尚未打开时，依次登记政策、研究族、候选和批次。
5. 先执行两次 `run-smoke`，比较两个运行清单中每个 `raw_factor.parquet` 的哈希；质量文件含实际耗时，整包哈希不要求相同。
6. 冒烟通过后切换到可见配置，执行一次 `doctor`、一次 `run-visible` 和一次 `ledger-verify`。

正式命令形态如下，真实路径和 ID 只保存在服务器私有配置中：

```bash
uv run factor-miner validate-spec candidate-v0.1.json
uv run factor-miner compile-spec candidate-v0.1.json
uv run factor-miner register-policy policy.json --artifact-root "$FM_ARTIFACT_ROOT"
uv run factor-miner register-family family.json --artifact-root "$FM_ARTIFACT_ROOT"
uv run factor-miner register-candidate candidate-v0.1.json --artifact-root "$FM_ARTIFACT_ROOT"
uv run factor-miner register-campaign campaign-v0.1.json --artifact-root "$FM_ARTIFACT_ROOT"
uv run factor-miner doctor --env-file server-smoke.env
uv run factor-miner run-smoke --campaign-id <campaign_id> --env-file server-smoke.env
uv run factor-miner doctor --env-file server-visible.env
uv run factor-miner run-visible --campaign-id <campaign_id> --env-file server-visible.env
uv run factor-miner ledger-verify --artifact-root "$FM_ARTIFACT_ROOT"
```

可见运行无论统计通过还是失败，只要合同核验、审计链和明确终态完整，都算一次可信执行。其输出仍只是“通过可信可见验证的候选因子”，没有 sealed OOS，也不能进入生产。
