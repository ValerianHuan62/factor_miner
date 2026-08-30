# 因子研究协议

## 1. 层级

```text
Data Contract
  → Raw Factor Compute
  → Evaluation Preprocess
  → Visible RankIC / Statistics
  → Redundancy Gate
  → Incremental Information Gate
  → Visible Candidate Package
```

原始因子层不得混入 evaluation preprocess。candidate expression 只能生成 raw factor。

## 2. DSL

候选表达式只允许字段引用、有限常数和以下白名单算子：

```text
add sub mul div neg abs
delay delta
rolling_sum rolling_mean rolling_std rolling_min rolling_max rolling_corr
```

允许窗口为 `{5, 10, 20, 40, 60, 120}`；最大节点数 15、最大深度 5、最大 lookback 130。禁止标签字段、负 shift、centered rolling、动态字段、未知算子和任意 Python。

除零结果定义为 null/NaN，不添加隐式 epsilon；缺失值保持缺失，不 `fillna(0)`。

## 3. 编译与计算

compiler 必须确定性地产生 Polars expression plan，并记录 canonical AST hash、required fields、lookback 和 plan hash。相同 spec、代码、配置和数据 release 必须产生相同 raw factor hash。

计算按 asset/date 排序，仅使用当前及历史数据。signal availability 与最早可交易时间必须在 profile 中声明；candidate 不得自行改变。

## 4. Evaluation

evaluation 从冻结的 profile 读取 mask、label、split 和预处理规则。公司 A 股 profile 使用 `valid_for_factor_rank` 计算截面 RankIC。原始 factor 先保留，评价阶段才执行冻结的截面处理。

每个日期至少满足 campaign 的 `min_names_per_date`；整体必须满足 `min_valid_dates` 和 `min_median_coverage`。不足时返回明确 failure code，不能改变区间或股票池重试同一 trial。

## 5. 冗余

第一阶段检查 canonical AST hash、字段/窗口签名和显式结构重复；第二阶段计算 candidate 与 campaign/reference pool 的逐日截面 Spearman，并汇总总体与分年度结果。超过冻结阈值时标记 `REDUNDANCY_THRESHOLD_EXCEEDED`，但保留全部 artifact 与事件。

## 6. 增量信息

逐对冗余之后执行联合线性冗余检查。程序对每天的候选和冻结参考基底做平均秩、样本标准化和带截距 OLS，只将无法被参考空间解释的残差交给结果评价。

参考库内容 ID、有序因子集合、清单 SHA-256、覆盖率、最低超额股票数、条件数、最低残差方差比例和最低残差 RankIC 均在 outcome 前冻结。同批次更早候选按预登记顺序进入联合基底。禁止使用 PCA、正则化、自适应选因子或结果后删列。

残差使用与原始因子相同的 RankIC、HAC 和完整研究族 Bonferroni 口径。残差通过不代表非线性独立、因果机制、可交易收益或样本外有效。

## 7. 状态

合法终态只有：

```text
visible_passed
visible_failed
compile_failed
compute_failed
evaluation_failed
redundancy_failed
incremental_failed
interrupted
```

所有失败必须有稳定错误码和非零 CLI 退出码；禁止静默切换数据、标签、窗口、mask 或统计方法。
