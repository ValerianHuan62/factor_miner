# Factor Miner V0.3 走步式 HMM 市场状态技术设计

- 状态：已批准实施
- 日期：2026-07-29
- 前置版本：V0.2 增量信息评价

## 1. 结论

V0.3 建立一个简单、时点正确、可失败关闭的日频市场状态日历。它使用公司 A 股全市场行情和状态表构造四个市场特征，以 Gaussian HMM 识别联合分布状态，并输出逐日过滤状态概率。这个日历用于后续比较因子在不同市场环境下的 RankIC、稳定性和覆盖率，不直接决定仓位，也不证明状态具有因果或交易价值。

研究阶段比较 `K=2、3、4`、两种收益聚合、三种训练窗口和两种协方差结构。BIC 只作诊断；优先比较走步式样本外对数似然、跨期稳定性和经济区分度。研究报告不能自动改变正式系统。人工批准一个候选后，正式运行固定状态数、特征定义、训练窗口和协方差结构，只在每月第一个交易日重新估计参数。

V0.3 不采用“收益接近但波动不同就合并”的规则。波动率本身可能改变杠杆、成本、回撤和因子表现。只有两个状态在发射分布、占比、持续时间和转移特征上同时近似，才判定为冗余。

## 2. 金融目的、使用方式与风险

### 2.1 要回答的问题

市场状态日历先回答：

1. 当前市场收益、波动、流动性和宽度的联合分布更接近哪个历史状态；
2. 在只使用当时可得信息时，各状态概率是多少；
3. 同一因子的每日 RankIC、覆盖率和稳定性是否随状态发生可重复的变化。

后续覆盖图谱至少比较：

\[
E[\operatorname{RankIC}_{f,t}\mid S_t=i]
\]

并允许经独立、版本化接口接入外部系统已计算的逐日组合指标，例如：

\[
E[R^{ridge3w}_t\mid S_t=i],\quad
E[\operatorname{Drawdown}_t\mid S_t=i],\quad
E[\operatorname{TurnoverCost}_t\mid S_t=i]
\]

`factor_miner` 不 import `huan_quant`，也不在 V0.3 内计算组合收益、成本、仓位或回撤。外部指标只能作为带来源、版本和截止日的逐日诊断序列接入。缺少外部指标不阻止生成市场状态日历，但不得声称状态已经改善策略。

### 2.2 结论边界

- HMM 状态是统计组件，不是客观存在且永久不变的“牛市、熊市、震荡市”。
- 状态标签跨月匹配只能降低编号漂移，不能保证长期经济语义固定。
- Gaussian HMM 可能用多个高斯近似金融数据的厚尾，虚构出没有决策价值的状态。
- 样本外似然较好不等于因子表现有区分，也不等于可以交易。
- 条件 RankIC 有差异只说明历史关联，不能证明市场状态是因子有效性的原因。
- V0.3 的可见区间仍是研究区间，不是密封样本外证据。

如果不同状态不能稳定区分后续因子或策略诊断，V0.3 仍可保留为研究产物，但不得进入正式决策接口。

## 3. 范围

### 3.1 本版包含

- 基于公司 A 股 point-in-time 股票池的四个日频市场特征；
- 训练窗口内标准化；
- 研究期 `K=2、3、4` 走步式比较；
- `expanding`、`rolling_5y` 和 `rolling_8y` 窗口比较；
- `diag` 和 `full` covariance 比较；
- 固定多 seed 拟合并保留训练似然最高的收敛解；
- 月度重新估计、月内固定参数；
- 项目自有的逐日 forward filter；
- 数值化质量闸门；
- Hungarian algorithm 跨月全局状态匹配；
- 正式运行期固定 `K` 的不可变部署 Spec；
- 内容寻址的模型、状态概率、诊断和清单产物；
- 合成数据测试与公司 Linux 真实验收。

### 3.2 本版不包含

- 宏观变量、新闻、分钟数据或深度状态模型；
- Student-t HMM、HSMM、切换回归或神经网络；
- 每月动态改变正式运行期的 `K`；
- HMM 自动生成经济状态名称；
- 仓位、回测、组合构建、成本或容量计算；
- 因子覆盖图谱和 LLM 假设生成；
- 用状态结果反向修改因子评价政策；
- 密封样本外检验、CPCV 或 Deflated Sharpe Ratio。

## 4. 两阶段治理

### 4.1 研究选择阶段

不可变 `RegimeResearchSpec` 冻结：

- 数据发布和状态表要求；
- 研究起止日与月度走步切分；
- 候选 `K=(2,3,4)`；
- 收益聚合候选 `("median", "equal_weight")`；
- 窗口候选 `("expanding", "rolling_5y", "rolling_8y")`；
- covariance 候选 `("diag", "full")`；
- 固定 seeds；
- `min_daily_assets=500`；
- `min_oos_months=24`；
- `min_successful_month_ratio=0.90`；
- 特征公式、标准化方法和缺失规则；
- 模型收敛参数；
- 质量阈值和状态距离；
- 候选排序规则；
- 下游诊断清单，但不把结果写入模型拟合。

研究阶段最多形成：

\[
3 \times 2 \times 3 \times 2=36
\]

个模型配置。每个配置的全部走步月份都使用相同 `K`、收益聚合、窗口和 covariance。某个月不能拟合时记录失败，不能换参数兜底。

研究输出 `RegimeResearchReport`。由于 median 和 equal-weight 收益是两个不同的被建模序列，其样本外 density score 不宜直接横向裁决；报告分别在两个收益定义内部给出确定性推荐，不自动给出跨收益定义的唯一冠军，也不能自动生成正式状态日历。

### 4.2 正式运行阶段

人工审阅研究报告后登记不可变 `RegimeDeploymentSpec`，明确固定：

- `K`；
- 收益聚合方式；
- 训练窗口；
- covariance；
- seeds 和拟合参数；
- 月度重新估计日；
- 特征、过滤、匹配和质量政策版本；
- 可使用的起始日和发布截止日。

正式运行只能读取已经登记的部署 Spec。每月重新估计参数，但不重新选择 `K`、窗口、收益聚合或 covariance。任何配置变化都必须产生新的部署 ID，不能覆盖旧日历。

## 5. Point-in-time 特征

### 5.1 当日合格股票池

日期 \(t\) 的基础集合为：

```text
valid_for_factor_rank = true
close 和 amount 为有限值
close > 0
amount >= 0
```

现有合同已经保证 `valid_for_factor_rank` 排除 ST、新股和停牌股票，并按当日状态生成，禁止使用当前仍上市股票回填历史。若状态表版本、截止日或 mask 合同不完整，整次运行硬失败。

一字涨跌停包含真实的市场压力信息，不从收益和市场宽度中静默删除。V0.3 根据 `can_buy`、`can_sell` 额外记录 `restricted_trading_ratio` 作为诊断，但不把它加入 HMM，避免本版继续扩张特征维度。

### 5.2 四个输入特征

#### 市场收益

研究阶段比较两种冻结定义：

```text
median：
  当日合格股票简单收益的截面中位数

equal_weight：
  当日合格股票简单收益的截面算术平均数
```

个股简单收益使用同一资产最近两个有效交易观察：

\[
r_{a,t}=\frac{close_{a,t}}{close_{a,t^-}}-1
\]

其中 \(t^-\) 是该资产不晚于 \(t\) 的上一个有效交易观察。不得用向后修复的未来价格填充。

#### 二十日实现波动率

\[
RV_{20,t}=\operatorname{std}
(r^{mkt}_{t-19},\ldots,r^{mkt}_{t})
\]

使用样本标准差，要求完整的 20 个市场交易日；不足时为空，不缩短窗口。

#### 对数相对成交额

先求当日合格股票总成交额：

\[
Amount_t=\sum_{a\in U_t} amount_{a,t}
\]

再计算：

\[
Liquidity_t=
\log\left(
\frac{Amount_t}{MA_{20}(Amount)_t}
\right)
\]

`MA20` 包含当日且要求完整 20 个市场交易日。分母非正、输入非有限或窗口不足时为空。对数相对量减弱市场长期扩容和价格水平趋势，但不能完全消除股票池结构变化，因此每日同时保存合格股票数。

#### 上涨股票占比

\[
Breadth_t=
\frac{\#\{a\in U_t:r_{a,t}>0\}}
{\#\{a\in U_t:r_{a,t}\text{ 有效}\}}
\]

分母低于冻结的 `min_daily_assets=500` 时当日特征不可用。涨停股票只要价格和状态有效，仍计入上涨家数。

### 5.3 训练窗口内标准化

每个重新估计月只使用训练窗口估计：

\[
z_{t,j}=\frac{x_{t,j}-\mu_{train,j}}{\sigma_{train,j}}
\]

均值和样本标准差在月内冻结。当月观察可以落在训练区间之外，不截断、不使用全样本统计量。任一训练特征标准差非有限或小于 `1e-12` 时，该模型月份硬失败。

## 6. 走步训练与月内推断

### 6.1 时间切分

每月第一个交易日重新估计。训练数据严格截止到前一交易日；当月只作推断。三种研究窗口为：

```text
expanding：
  从冻结研究起点到前一交易日

rolling_5y：
  前 1260 个可用市场交易日

rolling_8y：
  前 2016 个可用市场交易日
```

三种窗口都至少要求 1260 个完整特征日；`rolling_8y` 不足 2016 日时不可用，不降级为五年或 expanding。正式部署只保留人工批准的一种窗口。

### 6.2 多初始值拟合

V0.3 默认冻结：

```text
seeds = (11, 23, 47, 71, 101)
n_iter = 500
tol = 1e-4
min_covar = 1e-6
```

同一模型月份对每个 seed 独立拟合，只在收敛、参数有限且协方差通过正定检查的结果中保留训练对数似然最高者。不能跨月份临时增加 seed 或改变迭代参数。所有 seed 都失败时，该模型月份为 `unavailable`。

### 6.3 过滤概率

正式可用概率必须是：

\[
P(S_t\mid X_1,\ldots,X_t)
\]

不能使用：

\[
P(S_t\mid X_1,\ldots,X_T),\quad T>t
\]

`hmmlearn` 只用于参数估计。V0.3 根据冻结的初始概率、转移矩阵和 Gaussian 发射参数实现缩放版 forward recursion，逐日计算 filtered probability。禁止直接把整月 `predict`、Viterbi 路径或 forward-backward posterior 当作交易可用概率。

跨月第一个交易日的先验使用上一交易日 canonical filtered probability，经 Hungarian 映射转到新模型 raw state 后再执行一步转移和发射更新。首个可用月份先用拟合后的参数和 `startprob_` 对完整训练窗口执行项目自有 forward filter，再把训练窗口最后一天的 filtered probability 作为首个样本外交易日的先验。禁止用平滑概率初始化。

研究解释如确需 smoothed probability，必须写入独立且明确标记 `research_only=true` 的产物。正式 `RegimeSnapshot` 不包含平滑概率。

### 6.4 可得时点

日期 \(t\) 的特征需要 \(t\) 日收盘数据，因此：

```text
observation_date = t
computed_after = t 日收盘数据发布后
earliest_use_date = t 的下一交易日
```

任何下游条件统计必须按指标自己的观察时点对齐：

- 对日期 \(t\) 收盘后形成的因子信号，若标签从 \(t+1\) 开始，状态 \(t\) 与因子信号同时可得，可以用于该信号的条件 RankIC；
- 对交易日 \(t\) 盘中已经实现的组合收益、成本或回撤，只能使用 `earliest_use_date <= t` 的状态，通常是状态 \(t-1\)；
- 任何场景都不能把状态 \(t\) 假装成 \(t\) 日开盘前已知。

## 7. 研究期模型比较

### 7.1 BIC 的位置

训练月记录：

\[
BIC=-2\log L+p\log n
\]

其中 \(p\) 按 `K`、特征维度和 covariance 结构精确计算。BIC 只用于识别明显没有补偿复杂度的模型，不作为正式配置裁判。

### 7.2 评价优先级

只比较通过硬质量闸门、至少有 24 个样本外月份且成功月份比例不低于 90% 的候选。median 和 equal-weight 两个收益定义分别形成比较轨道；不同轨道不直接比较 likelihood。每个轨道内部的确定性推荐采用以下字典序：

1. 最大化月度样本外平均对数似然的中位数；
2. 最小化跨月 Hungarian 匹配成本的中位数；
3. 最大化月度最小状态间 Bhattacharyya 距离的中位数；
4. 最小化训练 BIC 的中位数；
5. 以规范候选 ID 排序打破完全相同结果。

每月标准化参数不同。计算跨月状态距离前，必须先把 Gaussian 参数还原到原始特征坐标：

\[
\mu^{raw}=\mu^{train}+\operatorname{diag}(\sigma^{train})\mu^z
\]

\[
\Sigma^{raw}=
\operatorname{diag}(\sigma^{train})
\Sigma^z
\operatorname{diag}(\sigma^{train})
\]

禁止直接比较两个不同训练 scaler 下的 z-space 均值或 covariance。上述排序对应：

```text
样本外有效性
  > 状态稳定性
  > 经济区分度
  > BIC
```

推荐只是研究报告字段。人工还要审阅状态占比、持续时间、条件收益、波动、下行波动、宽度、流动性和下游诊断，才能登记部署 Spec。

### 7.3 防止循环选择

候选排序的前三项只使用市场特征和模型概率，不使用任何候选因子 RankIC、组合收益或成本。下游指标只作部署前的独立实用性检查，防止为了让某个已有因子看起来有状态差异而挑选 `K`。

如果市场模型通过统计质量，但已登记的下游诊断在状态间没有稳定差异，研究报告必须标记 `downstream_value_unverified`，不得宣称它对交易系统有增量。

条件摘要默认使用软概率权重：

\[
\widehat E[y_t\mid S_t=i]
=
\frac{\sum_t p_{t,i}y_t}{\sum_t p_{t,i}}
\]

同时保存 argmax 硬标签的样本数作为可读性诊断，但不以硬标签替代概率。不同指标仍必须遵守第 6.4 节的信息可得时点。

## 8. 数值化质量规则

### 8.1 收敛与数值

以下任一情况硬拒绝该模型月份：

- 优化器未收敛；
- 初始概率或转移矩阵非有限、为负或行和偏离 1 超过 `1e-8`；
- 任一自转移概率导致平均持续时间非有限；
- 均值或 covariance 非有限；
- 任一 full covariance 最小特征值小于 `1e-8`；
- 任一 diag covariance 小于 `1e-8`；
- filtered probability 非有限、为负或和偏离 1 超过 `1e-8`；
- 月内任一天无法形成完整四维特征。

### 8.2 状态占比

占比使用该训练窗口内 filtered probabilities 的平均值，不使用 Viterbi 硬标签：

```text
硬拒绝：任一状态占比 < 3%
警告：任一状态占比 < 8%
核心状态：占比 >= 8%
```

### 8.3 状态持续时间

状态 \(i\) 的隐含平均持续时间为：

\[
E[D_i]=\frac{1}{1-p_{ii}}
\]

```text
硬拒绝：任一核心状态平均持续时间 < 3 个交易日
警告：按状态占比加权的平均持续时间 < 5 个交易日
```

占比在 3% 至 8% 的稀有冲击状态不因持续时间短而单独硬拒绝，但仍保留警告和完整诊断。

### 8.4 状态相似度

两个多元高斯状态使用 Bhattacharyya distance：

\[
D_B(i,j)=
\frac18(\mu_i-\mu_j)^\top\Sigma^{-1}(\mu_i-\mu_j)
+\frac12\log
\frac{\det\Sigma}
{\sqrt{\det\Sigma_i\det\Sigma_j}},
\quad
\Sigma=\frac{\Sigma_i+\Sigma_j}{2}
\]

它同时比较均值和 covariance，因此收益均值接近但波动明显不同的状态不会被自动合并。

```text
相似警告：D_B(i,j) < 0.15

冗余硬拒绝需同时满足：
  D_B(i,j) < 0.05
  占比绝对差 < 0.03
  持续时间相对差 < 0.20
  转移特征 L1 距离 < 0.10
```

V0.3 不在拟合后合并状态。满足冗余条件时整个模型月份拒绝，防止事后改变已经冻结的 `K`。

状态 \(i\) 的转移特征定义为：

```text
(p_ii, 按从大到小排列的全部非自身转移概率)
```

因此两个状态的转移距离不依赖尚未完成的状态标签映射。

### 8.5 全部失败

如果一个正式月份所有 seed 都失败，或最终模型违反硬质量规则，则输出带失败码的 `unavailable` 月份，不沿用旧参数、不强制给出状态、不切换到另一个 `K` 或 covariance。

## 9. 跨月 canonical 状态映射

### 9.1 成本矩阵

旧模型 canonical 状态 \(i\) 与新模型 raw 状态 \(j\) 的匹配成本为：

\[
C_{ij}=
0.70\,\widetilde D_B(i,j)
+0.10\,D_{duration}(i,j)
+0.05\,D_{occupancy}(i,j)
+0.15\,D_{transition}(i,j)
\]

发射分布距离使用还原到同一原始特征坐标的均值和 covariance。各距离再按研究 Spec 冻结的截断上限缩放到 `[0,1]`：

```text
Bhattacharyya distance：上限 5
持续时间绝对对数比：上限 log(20)
占比绝对差：上限 1
转移行 L1 距离：上限 2
```

`D_transition` 比较上一节定义的转移特征，不在构造成本矩阵时假设未知映射。实现构造完整 `K × K` 成本矩阵，并使用 Hungarian algorithm 最小化总成本，禁止逐一贪心匹配。匹配完成后可以额外保存按 canonical 顺序重排的完整转移矩阵差异，但它只作诊断，不反向改变本次映射。

### 9.2 初次编号

首个正式月份没有旧模型，按训练窗口内各状态的条件市场收益从低到高编号；完全相同时依次按条件实现波动率从高到低、raw state ID 从小到大打破平局。此排序只建立初始编号，不把状态永久命名为牛熊。

### 9.3 保存两类身份

逐月模型和逐日结果同时保存：

```text
raw_state_id
canonical_state_id
state_mapping_cost
mapping_policy_version
```

人工经济解释是引用具体 snapshot 和 canonical state 的独立注释，不覆盖原始统计，也不随下一月自动继承。

## 10. 冻结对象与产物

### 10.1 规范对象

新增内容寻址对象：

```text
RegimeResearchSpec
RegimeDeploymentSpec
RegimeDiagnosticSeriesManifest
RegimeAnnotation
```

全部使用规范 JSON 和 SHA-256 身份，登记后不可原地修改。

### 10.2 研究产物

```text
regime_research_report.json
research_candidates.jsonl
monthly_fit_diagnostics.parquet
oos_log_likelihood.parquet
state_stability.parquet
conditional_market_profiles.parquet
downstream_diagnostics.parquet
```

`downstream_diagnostics.parquet` 只能读取清单登记的聚合逐日序列，记录来源系统、数据版本、算法版本、截止日和 SHA-256。不得包含个股数据、仓位或公司密钥。

### 10.3 正式状态快照

`RegimeSnapshot` 为内容寻址、不可变目录：

```text
manifest.json
deployment_spec.json
monthly_models.jsonl
filtered_regimes.parquet
market_features.parquet
quality_diagnostics.json
annotations.jsonl
```

`filtered_regimes.parquet` 至少包含：

```text
observation_date
earliest_use_date
model_month
model_id
raw_state_id
canonical_state_id
raw_state_probabilities
canonical_state_probabilities
max_probability
normalized_entropy
status
failure_code
```

归一化熵定义为：

\[
H_t=-\frac{\sum_i p_{t,i}\log p_{t,i}}{\log K}
\]

`max_probability` 是置信度的简单摘要；不得把低熵解释为模型一定正确。

`market_features.parquet` 保存原始四个特征、训练窗口 z-score、合格股票数和 `restricted_trading_ratio`。`manifest.json` 绑定数据发布、清单哈希、结构版本、市场截止日、复权口径、交易日历、状态表、代码提交、配置哈希和全部文件哈希。

## 11. 领域接口与 CLI

建议新增纯领域接口：

```python
build_market_features(...)
fit_monthly_gaussian_hmm(...)
filter_state_probabilities(...)
evaluate_regime_quality(...)
match_canonical_states(...)
compare_regime_candidates(...)
summarize_daily_diagnostics_by_regime(...)
publish_regime_snapshot(...)
```

正式 CLI：

```text
factor-miner regime research
factor-miner regime register-deployment
factor-miner regime build
factor-miner regime verify
```

所有真实命令只允许在公司 Linux 服务器执行。Mac 只运行程序生成的合成数据测试。

## 12. 失败语义

至少新增以下稳定失败码：

```text
REGIME_FEATURE_INCOMPLETE
REGIME_TRAINING_HISTORY_INSUFFICIENT
REGIME_STANDARDIZATION_INVALID
REGIME_FIT_NOT_CONVERGED
REGIME_COVARIANCE_INVALID
REGIME_TRANSITION_INVALID
REGIME_STATE_OCCUPANCY_TOO_LOW
REGIME_CORE_DURATION_TOO_SHORT
REGIME_STATES_REDUNDANT
REGIME_FILTER_INVALID
REGIME_MAPPING_INVALID
REGIME_ALL_CANDIDATES_UNAVAILABLE
REGIME_DOWNSTREAM_MANIFEST_MISMATCH
```

研究候选局部失败进入报告；正式月份失败输出 `unavailable`。数据合同、清单、部署 Spec 或文件哈希错误则整次运行硬失败。

## 13. 开源依赖决策

V0.3 拟采用：

- `hmmlearn 0.3.3`：只用于 Gaussian HMM 参数拟合、训练似然和 BIC 基础能力；
- `scipy.optimize.linear_sum_assignment`：用于 Hungarian 全局匹配；
- 项目自有代码：forward filter、状态质量、距离、时间切分、产物和审计。

截至 2026-07-29，[`hmmlearn` 官方仓库](https://github.com/hmmlearn/hmmlearn)约 3.4k stars，[PyPI](https://pypi.org/project/hmmlearn/)最新稳定版为 0.3.3，发布于 2024-10-31；官方仓库明确标记为 limited-maintenance mode。它具备成熟的 Gaussian HMM 拟合接口，但维护状态不适合承载项目关键语义。[SciPy `linear_sum_assignment` 官方文档](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.linear_sum_assignment.html)明确提供线性和分配问题求解。项目不得依赖 `hmmlearn` 高层 `predict` 输出作为正式 filtered probability，并必须用合成测试锁定取参兼容性。升级依赖时重新执行数值回归和服务器验收。

## 14. 测试与验收

### 14.1 Mac 合成测试

- 两状态相同收益、不同波动时不被错误判为冗余；
- 均值、covariance、占比、持续时间和转移均相近时触发冗余拒绝；
- 全样本标准化与训练窗口标准化产生不同结果，正式实现只接受后者；
- 修改未来月份数据不改变历史 filtered probability；
- smoothed posterior 无法进入正式快照；
- 月内参数保持不变；
- 正式部署不能改变 `K`；
- 不同 seed 只选择最高似然的合格解；
- 占比、持续时间、正定性和概率归一化阈值逐项生效；
- Hungarian 全局匹配在贪心会失败的成本矩阵上得到最小总成本；
- 月度失败输出 `unavailable`，不复用旧模型；
- `earliest_use_date` 永远是下一交易日；
- 任一数据版本、Spec 或产物变化都会改变快照 ID；
- 核心包和 CLI 不 import `huan_quant` 或个人 Skill。

### 14.2 公司 Linux 验收

真实验收至少运行一个完整研究配置集和一个固定部署配置，检查：

- point-in-time 股票池、特征公式和 20 日窗口抽样对账；
- 月度训练截止日严格早于推断月；
- 研究候选的 OOS likelihood、稳定性、BIC 和质量报告完整；
- 正式运行所有月份固定 `K`、窗口、收益聚合和 covariance；
- 逐日概率和为 1，raw/canonical 映射可追溯；
- 结果日最早只能在下一交易日使用；
- 至少一个因子每日 RankIC 序列可通过版本化接口形成状态条件摘要；
- 失败月份不会被静默填充；
- provenance 和全部哈希可以由 `regime verify` 复核。

真实结果只能称为“通过 V0.3 市场状态日历验收”，不能称为已经提高策略收益或发现可交易 regime。

## 15. 后续版本

- V0.4：使用冻结 `RegimeSnapshot` 构建数学形式与实际信号模式双层因子覆盖图谱；每日 IC 相关性必须逐因子对独立计算，不允许把全部因子 IC 序列拼成矩阵后一次性相关。
- V0.5：使用冻结覆盖图谱生成经过脱敏与审计的 LLM 覆盖摘要，再提出可证伪假设和白名单 DSL 候选。
- 后续版本再处理公共表达式 DAG、分层缓存、研究记忆、调度恢复、策略级 CPCV、Deflated Sharpe Ratio 和密封样本外检验。
