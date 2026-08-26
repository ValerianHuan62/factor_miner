# 可移植研究治理

## 1. 研究对象

当前版本只维护结构化研究规范、不可变登记对象、`TrialEvent` 和结果产物。`Result` 不等于 Evidence；当前版本不创建 Conclusion、Mental Model 或 Research Rule。

## 2. Hypothesis 准入

候选在 outcome 前必须声明：

- claim 与 expected sign
- 经济机制及可观察 proxy
- 独立机制验证所需的数据与方法
- competing explanations
- baseline/reference factor
- failure modes 与 falsification path
- 精确、可核验的来源引用

缺任何一项时只能保存 draft，不允许进入 visible campaign。经济机制尚未验证时必须标记 `mechanism_unverified`；统计结果不得反向证明叙事。

## 3. Campaign 与 trial budget

每次 visible campaign 必须在读取 outcome 前冻结：候选集合、最大 hypothesis 数、可见区间、universe、label、mask、HAC 参数、显著性 alpha、Bonferroni family size、数据质量阈值和冗余阈值。

每个公式、窗口或参数变体占用一个 slot。编译失败、全 NaN、低覆盖、显著性失败和冗余失败均保留 TrialEvent；不得删除失败者后缩小 family。

## 4. 可见验证

V0 的统计闸门为：

1. 按日计算截面 Spearman RankIC。
2. 使用 campaign 冻结的 HAC max lags 估计均值显著性，报告双侧 raw p 值。
3. 使用预登记 `max_hypotheses` 做 Bonferroni：`p_adjusted = min(1, p_raw * max_hypotheses)`。
4. expected sign、数据质量、adjusted p 和冗余 Gate 全部通过，才可标记 `visible_passed`。

通过只说明该候选值得进入后续 sealed OOS；不得据此宣称存在可交易 Alpha。

V0.2 在上述原始检验之后增加增量信息闸门。冻结参考库在 outcome 前登记并以内容哈希绑定；候选对完整参考空间做每日截面正交化，残差再执行同口径 RankIC、HAC 和全研究族 Bonferroni。原始通过而残差不通过时，终态必须是 `incremental_failed`，不得降级宣传为 V0.1 通过。

正交化只回答相对于冻结参考库的线性增量。它不证明机制、非线性独立、密封样本外表现或交易收益。

## 5. 测试与结果后行为

V0 不访问 sealed test。任何根据 visible 结果进行的修复必须产生新的 candidate ID、parent lineage 和新的 hypothesis slot。结果后解释保存在独立字段，不得覆盖事前假设。

## 6. 大语言模型边界

V0 不调用 LLM。未来 LLM 只能提议结构化 Spec；不得修改 compiler、evaluator、统计 family、数据 split 或结果。公司原始数据、个股结果和完整评价不得发送给外部模型。
