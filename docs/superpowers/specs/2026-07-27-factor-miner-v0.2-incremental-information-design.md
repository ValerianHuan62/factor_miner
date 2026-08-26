# Factor Miner V0.2 增量信息评价技术设计

- 状态：已批准实施
- 日期：2026-07-27
- 前置版本：V0.1 可信可见验证

## 1. 结论

V0.2 只解决一个研究问题：一个通过原始可见检验的新候选，在剔除冻结参考因子库及同批次更早候选能够线性解释的部分后，是否仍保留方向一致、统计显著且具有最低效果量的截面预测信息。

V0.2 不修改 V0.1 的原始因子、历史政策、批次、账本或产物。V0.2 新增内容寻址的参考因子库和正交化政策，使用截面排名 OLS 生成评价层残差，再对残差执行 RankIC、HAC 和原研究族 Bonferroni 校正。通过结果只能称为“通过可见增量信息验证的候选因子”，不是独立 Alpha、认证因子、Evidence 或生产结论。

## 2. 金融含义与结论边界

现有逐对相关性只能回答“新候选是否与某一个参考因子高度相似”，不能排除新候选是多个低相关参考因子的线性组合。V0.2 将新候选在每天的股票截面上投影到完整冻结参考基底：

\[
z(f^{new}_t)=\alpha_t+z(X^{ref}_t)\beta_t+\varepsilon_t
\]

其中 \(z\) 表示先做平均秩，再做样本标准化；\(\varepsilon_t\) 是当天无法被参考基底线性解释的部分。回归只读取当日候选、参考因子和时点正确的股票池，不读取未来收益。未来收益只在残差生成后由独立 `OutcomeSource` 打开。

V0.2 分别回答：

1. `R²`：已有因子空间能解释新候选多少截面变化；
2. `1-R²`：新候选保留多少线性独立变化；
3. 残差 RankIC：独立变化是否仍与未来收益排序有关；
4. 残差 HAC 与 Bonferroni：该关系在时间依赖和完整研究族尝试次数下是否仍显著。

线性正交化不能排除非线性冗余，也不能证明因果机制或可交易收益。因此 V0.2 保留规范 AST、逐对 Spearman 和联合正交化三道不同闸门，且不替代后续密封样本外检验。

## 3. 范围

### 本版包含

- 内容寻址的 `ReferenceFactorLibrarySpec`
- 冻结的 `OrthogonalizationPolicySpec`
- V0.2 `IncrementalEvaluationPolicySpec`
- 每日截面平均秩、标准化、带截距 OLS 与确定性残差
- 参考覆盖率、设计矩阵秩、条件数、`R²` 和残差方差比例检查
- 残差 RankIC、年度诊断、HAC 与研究族 Bonferroni
- 冻结参考库加同批次更早候选组成的有序联合基底
- `incremental_information.json` 和候选包增量信息声明
- 旧 V0.1 政策与运行入口的不可变兼容
- 纯合成测试与公司 Linux 服务器真实验收

### 本版不包含

- 在线大语言模型、研究记忆或自动变异
- 公共表达式 DAG、分层缓存或增量计算
- PCA、Ridge、Lasso 或非线性残差模型
- 分组组合、换手、成本、容量或风格归因
- CPCV、Deflated Sharpe Ratio 或密封样本外检验
- PostgreSQL、任务队列、多写入器或分布式工作进程

## 4. 冻结对象

### 4.1 参考因子库

`ReferenceFactorLibrarySpec` 冻结：

- 参考数据清单 ID；
- 参考数据清单 SHA-256；
- 有序且唯一的参考因子 ID；
- 因子值列固定为 `raw_factor`；
- 基底选择固定为 `frozen_full_basis`；
- `point_in_time=true`；
- 带时区创建时间和可核验来源。

其规范 JSON 生成 `reflib_<24位哈希>`。政策中的旧式 `reference_manifest_id` 和 `reference_factor_ids` 必须与参考库逐字一致，防止逐对冗余和联合正交化使用不同参考集合。

### 4.2 正交化政策

V0.2 公司 A 股政策固定：

- `method="cross_sectional_rank_ols"`；
- `include_intercept=true`；
- `min_reference_coverage=0.80`；
- `min_cross_sectional_excess_names=20`；
- `max_condition_number=1e8`；
- `min_median_residual_variance_ratio=0.05`；
- `min_abs_mean_residual_rank_ic=0.01`。

候选不得覆盖这些值。任何参数变化都会产生新的评价政策 ID，并且后续候选仍占用研究族名额。

### 4.3 多重检验

原始 RankIC 和残差 RankIC 是同一个候选的层级式准入条件，不把一个候选拆成两个可回收试验名额；研究族预算仍按候选及其参数变体计数。两项推断都使用同一个冻结全局预算进行 Bonferroni 校正，不能因为原始结果失败、计算失败或残差失败而缩小检验族。

## 5. 每日截面算法

对每个可见交易日：

1. 使用 `valid_for_factor_rank=true` 的时点正确股票；
2. 对候选和全部基底取有限值完整交集，禁止在原始层或正交化层 `fillna(0)`；
3. 要求完整交集覆盖率不低于冻结阈值；
4. 要求股票数至少为 `参考列数 + 截距 + excess_names`；
5. 对候选及每个基底分别计算平均秩，再使用样本标准差标准化；
6. 构造带截距设计矩阵，要求满列秩且条件数不超过冻结阈值；
7. 使用 `numpy.linalg.lstsq` 计算 OLS；
8. 保存残差、`R²`、残差方差比例、样本数、矩阵秩和条件数；
9. 常数候选、常数参考、秩不足、非有限结果或数值不稳定均硬失败。

同批次候选按结果前冻结顺序处理。当前候选的联合基底由参考因子库和所有更早候选的原始因子组成；更早候选之后是否统计通过，不影响它已经占据的参考位置。

## 6. 增量信息评价

全部日期残差生成后才打开标签，并将残差列作为独立评价输入：

```text
orthogonal_residual
  → visible RankIC
  → 最低有效日期与覆盖率
  → HAC
  → 研究族 Bonferroni
  → expected sign
  → 最低平均残差 RankIC
```

V0.2 候选通过要求：

1. V0.1 原始 RankIC 的方向、效果量、HAC/Bonferroni 和覆盖率通过；
2. 规范 AST 与逐对输出冗余通过；
3. 每日联合正交化合同完整执行；
4. 残差方差比例中位数不低于政策阈值；
5. 残差 RankIC 方向与事前 `expected_sign` 一致；
6. 残差 RankIC 绝对均值不低于政策阈值；
7. 残差 HAC 的 Bonferroni p 值不高于冻结 `alpha`。

原始通过但增量失败的候选终态为 `incremental_failed`，保留全部原始和残差产物，不得退回称为 V0.1 通过候选。

## 7. 接口与产物

新增领域接口：

```python
def orthogonalize_candidate(
    candidate_frame: pl.DataFrame,
    reference_frames: Mapping[str, pl.DataFrame],
    eligibility_frame: pl.DataFrame,
    campaign: CampaignSpec,
    policy: OrthogonalizationPolicySpec,
) -> OrthogonalizedFactor: ...

def evaluate_incremental_information(
    orthogonalized: OrthogonalizedFactor,
    outcome_frame: pl.DataFrame,
    campaign: CampaignSpec,
    expected_sign: ExpectedSign,
    policy: OrthogonalizationPolicySpec,
) -> IncrementalInformationResult: ...
```

V0.2 每个候选新增：

```text
candidates/<candidate_id>/incremental_information.json
```

文件必须记录有序基底 ID、参考库 ID、每日诊断摘要、残差 RankIC 评价、HAC 推断、通过状态和明确失败原因。残差矩阵不作为新的原始因子发布；如未来需要把残差本身作为可复用生产特征，必须登记新的派生因子规范。

候选包新增：

```text
incremental_information_evaluated=true
incremental_information_passed=<bool>
reference_factor_library_id=<reflib_id>
visible_incremental_only=true
```

仍保留：

```text
sealed_oos_used=false
production_eligible=false
mechanism_status=mechanism_unverified
```

## 8. 失败语义

新增稳定失败代码：

- `REFERENCE_LIBRARY_MISMATCH`
- `ORTHOGONALIZATION_COVERAGE_LOW`
- `ORTHOGONALIZATION_RANK_DEFICIENT`
- `ORTHOGONALIZATION_NUMERIC_UNSTABLE`
- `INCREMENTAL_INFORMATION_INSUFFICIENT`

参考库缺失、顺序不符、哈希身份不符或数据截止日期不一致时整次运行硬失败。单日股票数、覆盖率或矩阵条件不满足时，该日记录为无效；有效日期不足时候选 `evaluation_failed`。合同已经完整执行但残差效果量、方向、显著性或独立方差不足时，候选 `incremental_failed`。

## 9. 兼容与迁移

- V0.1 `EvaluationPolicySpec(policy_version="1")`、campaign、family、账本和产物保持原字节不变。
- V0.2 使用 `IncrementalEvaluationPolicySpec(policy_version="2")`，候选仍使用 V0.1 已验证的 `TrustedCandidateFactorSpec(spec_version="2")`，因为原始因子身份没有变化。
- 同一个候选使用 V0.2 政策时必须登记新的 research family 和 campaign，不能复用已经打开 V0.1 outcome 的试验名额。
- V0.1 CLI 运行语义保持可重放；V0.2 CLI 根据登记政策版本选择增量信息工作流。

## 10. 验收标准

- 合成数据证明一个由多个参考因子线性组合得到的新候选，即使逐对相关性均低于阈值，也会因残差方差不足而失败。
- 合成数据证明加入独立预测分量的候选具有非零残差、方向正确的残差 RankIC，并能通过 HAC/Bonferroni。
- 标签内容变化不能改变已经生成的正交残差。
- 参考因子顺序、数量、清单或库 ID 不匹配时，在打开标签前失败。
- 缺失值不被填零；完整交集覆盖率不足时失败。
- 秩亏或条件数超限不会使用伪逆静默继续。
- 原始失败、逐对冗余失败和增量失败具有不同终态及产物。
- V0.1 全部既有测试继续通过。
- Mac 只运行程序生成的合成测试；真实 V0.2 IC、正交化和显著性只在公司 Linux 服务器运行。

## 11. 后续版本

- V0.3 构建走步式、正式运行期固定 `K` 的 HMM 市场状态日历；
- V0.4 使用冻结状态快照构建数学形式与实际信号模式双层覆盖图谱；
- V0.5 使用冻结图谱快照驱动受控 LLM 假设和 DSL 候选生成；
- V0.6 实现批量公共表达式 DAG、分层缓存、严格失效和统一 Benchmark；
- V0.7 实现候选级恢复、调度和结构化研究记忆。

V0.3 至 V0.5 的候选路线记录在 `docs/superpowers/plans/2026-07-28-factor-miner-v0.3-v0.5-regime-coverage-and-llm.md`。组合构建、成本容量、CPCV、Deflated Sharpe Ratio 和密封样本外检验继续保留到具备完整策略收益与权限隔离的后续版本。
