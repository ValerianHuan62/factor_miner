# Factor Miner V0.3 走步式 HMM 市场状态实施计划

> **代理执行要求：**实施时必须使用 `superpowers:executing-plans`，逐项完成本计划。步骤使用复选框（`- [ ]`）跟踪。项目明确禁止使用子代理执行。

**目标：**实现一个使用公司 A 股 point-in-time 数据、研究期比较模型而正式运行期固定 `K`、只输出可交易时点正确 filtered probability 的日频 HMM 市场状态日历。

**架构：**V0.3 将市场特征、Gaussian HMM 参数拟合、项目自有 forward filter、状态质量、Hungarian 映射、研究选择和正式快照分成独立模块。`hmmlearn` 只负责拟合参数，所有时点语义、状态过滤、拒绝规则和产物身份由 `factor_miner` 控制。研究报告与正式部署 Spec 分离，研究结果不能自动改变正式系统。

**技术栈：**Python 3.12、Pydantic 2、Polars 1、NumPy、SciPy、hmmlearn 0.3.3、Typer、unittest。

## 全局约束

- 所有 Markdown 标题和正文使用中文；代码标识、路径、命令和标准缩写可以保留原文。
- Mac 只运行程序生成的合成数据测试；真实特征、HMM、状态概率和条件统计只在公司 Linux 服务器运行。
- 正式运行期固定 `K`、收益聚合、训练窗口和 covariance；每月只重新估计参数。
- 日期 \(t\) 收盘形成的状态最早在下一交易日使用。
- 正式路径只能使用 filtered probability；smoothed probability 不进入 `RegimeSnapshot`。
- 每个月的标准化统计量只能由该月训练窗口估计。
- 研究期 BIC 只作诊断；候选排序优先级为样本外有效性、状态稳定性、经济区分度、BIC。
- Gaussian 状态冗余必须同时比较均值、covariance、占比、持续时间和转移特征。
- 真实数据合同、版本、截止日、复权口径、交易日历、状态表、代码提交和配置哈希必须进入快照身份。
- `factor_miner` 不 import `huan_quant`，不计算策略仓位、组合收益、成本、容量或回撤。
- 真实数据、完整结果、运行配置、账本、模型和密钥不进入 Git。
- 每个任务执行测试先行，确认预期失败后才写生产代码；每个任务测试通过后单独提交。

---

### 任务 1：冻结 HMM 研究、部署和诊断合同

**文件：**
- 新增：`src/factor_miner/regime_schema.py`
- 修改：`src/factor_miner/errors.py`
- 修改：`pyproject.toml`
- 修改：`uv.lock`
- 新增：`tests/test_regime_schema.py`

**接口：**
- 产出：`ReturnAggregation`
- 产出：`TrainingWindow`
- 产出：`CovarianceKind`
- 产出：`RegimeFeaturePolicy`
- 产出：`RegimeQualityPolicy`
- 产出：`RegimeResearchSpec`
- 产出：`RegimeDeploymentSpec`
- 产出：`RegisteredRegimeResearch`
- 产出：`RegisteredRegimeDeployment`
- 产出：`RegimeDiagnosticSeriesManifest`
- 产出：`RegimeAnnotation`
- 产出：`regime_research_id(spec) -> str`
- 产出：`regime_deployment_id(spec) -> str`
- 产出：设计中全部 `REGIME_*` 稳定失败码。

- [x] **步骤 1：编写 schema 内容身份失败测试**

测试手工构造的研究 Spec 固定：

```python
RegimeResearchSpec(
    spec_version="1",
    candidate_state_counts=(2, 3, 4),
    return_aggregations=("median", "equal_weight"),
    training_windows=("expanding", "rolling_5y", "rolling_8y"),
    covariance_kinds=("diag", "full"),
    seeds=(11, 23, 47, 71, 101),
    n_iter=500,
    tol=1e-4,
    min_covar=1e-6,
    min_daily_assets=500,
    min_oos_months=24,
    min_successful_month_ratio=0.90,
    feature_policy=RegimeFeaturePolicy(),
    quality_policy=RegimeQualityPolicy(),
    research_start=date(2010, 1, 1),
    research_end=date(2026, 6, 30),
    created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
)
```

断言内容相同则 ID 相同；修改 `K`、seed、窗口、阈值或截止日都会改变 ID；重复 seed、未排序 `K`、未知 covariance、无时区时间、动态 `K` 部署和研究集合之外的正式配置都被拒绝。

- [x] **步骤 2：运行测试并确认模块缺失**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_regime_schema -v
```

预期：因 `factor_miner.regime_schema` 不存在而失败。

- [x] **步骤 3：增加直接依赖并更新锁文件**

在 `pyproject.toml` 增加：

```toml
"hmmlearn>=0.3.3,<0.4",
"scipy>=1.17,<2",
```

运行：

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv lock
```

锁文件必须解析出 `hmmlearn 0.3.3`、兼容 Python 3.12 的 `scikit-learn` 和 SciPy，不采用未锁定 Git 依赖。

- [x] **步骤 4：实现不可变 Pydantic 合同和内容 ID**

所有模型使用：

```python
model_config = ConfigDict(frozen=True, extra="forbid")
```

正式部署只允许单个：

```python
state_count: Literal[2, 3, 4]
return_aggregation: ReturnAggregation
training_window: TrainingWindow
covariance_kind: CovarianceKind
```

默认质量政策精确冻结设计中的占比、持续时间、正定性、Bhattacharyya、映射权重和截断上限。内容 ID 使用现有 `sha256_json`，前缀分别为 `regresearch_` 和 `regdeploy_`。

- [x] **步骤 5：运行 schema 测试和完整回归**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_regime_schema -v
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest discover -s tests -v
```

- [x] **步骤 6：提交**

```bash
git add pyproject.toml uv.lock src/factor_miner/regime_schema.py \
  src/factor_miner/errors.py tests/test_regime_schema.py
git commit -m "feat: freeze v0.3 regime contracts"
```

### 任务 2：构建时点正确的市场特征

**文件：**
- 新增：`src/factor_miner/regime_features.py`
- 新增：`src/factor_miner/regime_data.py`
- 新增：`tests/test_regime_features.py`
- 修改：`tests/helpers.py`

**接口：**
- 依赖：`RegimeFeaturePolicy`
- 依赖：现有 `DataProvenance` 和公司 A 股 market/state 合同。
- 产出：`RegimeDataSource`
- 产出：`CompanyAShareRegimeDataSource`
- 产出：`MarketFeatureFrame`
- 产出：`build_market_features(panel: pl.DataFrame, policy: RegimeFeaturePolicy, aggregation: ReturnAggregation, *, min_daily_assets: int) -> MarketFeatureFrame`
- 产出：`fit_training_standardizer(features: pl.DataFrame) -> TrainingStandardizer`
- 产出：`transform_market_features(features: pl.DataFrame, standardizer: TrainingStandardizer) -> np.ndarray`

- [x] **步骤 1：编写 point-in-time 股票池失败测试**

生成 520 只合成股票和 25 个交易日，人工设置 ST、新股、停牌、无效价格及 `can_buy/can_sell`。断言：

- ST、新股和停牌不进入市场收益、成交额或宽度；
- 一字涨跌停仍进入收益和宽度；
- `restricted_trading_ratio` 单独记录；
- 当日有效股票少于 500 时当日四维特征为空并返回 `REGIME_FEATURE_INCOMPLETE` 诊断；
- 修改未来日期的股票状态和价格不改变过去特征。

- [x] **步骤 2：运行测试并确认特征接口缺失**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_regime_features.MarketFeatureTest.test_point_in_time_universe_excludes_invalid_names -v
```

预期：因 `build_market_features` 不存在而失败。

- [x] **步骤 3：实现逐资产收益和四个市场特征**

先按 `asset,date` 排序，用上一有效观察计算简单收益，再按日过滤 `valid_for_factor_rank=true`。输出固定列：

```text
date
market_return
realized_volatility_20d
log_amount_relative_20d
advancers_ratio
eligible_assets
restricted_trading_ratio
feature_valid
failure_code
```

20 日窗口必须完整，使用样本标准差；成交额分母非正、收益非有限或日股票数不足都不能缩短窗口或填零。

- [x] **步骤 4：编写训练期标准化和未来不变性失败测试**

用手工可核验的三列小矩阵断言训练均值、样本标准差和 z-score。把巨大异常值追加到测试月后，断言旧训练 scaler 和旧日期 z-score 完全不变。常数列和非有限标准差必须抛出 `REGIME_STANDARDIZATION_INVALID`。

- [x] **步骤 5：实现专用 input-only 数据源**

`CompanyAShareRegimeDataSource` 只读 market/state，不读 label；验证 `date,asset` 唯一、market/state 键一致、全部状态 mask 与基础状态一致、截止日一致，并返回 `close,amount,is_st,is_newly_listed,is_suspended,can_buy,can_sell,valid_for_factor_rank`。不得运行时 import `huan_quant`。

- [x] **步骤 6：运行目标测试和完整回归**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_regime_features -v
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest discover -s tests -v
```

- [x] **步骤 7：提交**

```bash
git add src/factor_miner/regime_features.py src/factor_miner/regime_data.py \
  tests/test_regime_features.py tests/helpers.py
git commit -m "feat: build point in time regime features"
```

### 任务 3：实现多 seed Gaussian HMM 与纯过滤推断

**文件：**
- 新增：`src/factor_miner/regime_model.py`
- 新增：`tests/test_regime_model.py`

**接口：**
- 依赖：`TrainingStandardizer`
- 依赖：`RegimeResearchSpec` 或 `RegimeDeploymentSpec` 中的拟合参数。
- 产出：`GaussianHMMParameters`
- 产出：`MonthlyHMMFit`
- 产出：`FilteredStateSeries`
- 产出：`fit_gaussian_hmm(values: np.ndarray, state_count: int, covariance_kind: CovarianceKind, seeds: tuple[int, ...], n_iter: int, tol: float, min_covar: float) -> MonthlyHMMFit`
- 产出：`gaussian_log_emission(values: np.ndarray, parameters: GaussianHMMParameters) -> np.ndarray`
- 产出：`filter_state_probabilities(values: np.ndarray, parameters: GaussianHMMParameters, initial_probability: np.ndarray | None = None) -> FilteredStateSeries`
- 产出：`score_filtered_log_likelihood(...) -> float`

- [x] **步骤 1：编写手算二状态 forward filter 失败测试**

使用固定的二状态一维参数：

```python
start = [0.6, 0.4]
transition = [[0.8, 0.2], [0.3, 0.7]]
means = [[-1.0], [1.0]]
variances = [[0.25], [0.25]]
observations = [[-1.0], [0.9]]
```

用独立手算字面量断言每一天 filtered probability 和总 log likelihood。追加第三天数据不得改变前两天概率。

- [x] **步骤 2：运行测试并确认过滤器缺失**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_regime_model.RegimeModelTest.test_forward_filter_matches_hand_calculation -v
```

- [x] **步骤 3：实现缩放版 forward recursion**

每一天执行：

```python
prior = start_probability if t == 0 else filtered[t - 1] @ transition
unnormalized = prior * emission_probability
scale = unnormalized.sum()
filtered[t] = unnormalized / scale
log_likelihood += log(scale)
```

Gaussian density在 log-space 计算，再减当日最大 log emission 防止下溢。概率、矩阵和 scale 非有限或不满足归一化时抛出稳定领域错误。正式接口不调用 `predict`、`predict_proba` 或 forward-backward posterior。

- [x] **步骤 4：编写多 seed 选择和参数兼容性失败测试**

用固定合成三状态数据拟合。断言：

- 每个固定 seed 独立运行；
- 只保留收敛、有限、协方差有效的结果；
- 选择训练 log likelihood 最高者；
- 相同输入和 seed 得到相同参数；
- `diag` 和 `full` covariance 都规范化为 `(K,D,D)`；
- 所有 seed 失败时抛出 `REGIME_FIT_NOT_CONVERGED`。

- [x] **步骤 5：实现 hmmlearn 适配层**

只从 `GaussianHMM` 读取：

```text
startprob_
transmat_
means_
covars_
monitor_.converged
score(X)
bic(X)
```

立即复制成项目自有冻结参数对象，之后所有推断只走项目过滤器。记录 seed、迭代次数、训练 likelihood、BIC 和依赖版本。

- [x] **步骤 6：运行目标测试和完整回归**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_regime_model -v
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest discover -s tests -v
```

- [x] **步骤 7：提交**

```bash
git add src/factor_miner/regime_model.py tests/test_regime_model.py
git commit -m "feat: fit and filter gaussian regimes"
```

### 任务 4：实现状态质量与 Hungarian 全局匹配

**文件：**
- 新增：`src/factor_miner/regime_quality.py`
- 新增：`tests/test_regime_quality.py`

**接口：**
- 依赖：`GaussianHMMParameters`
- 依赖：`FilteredStateSeries`
- 依赖：`RegimeQualityPolicy`
- 产出：`StateDiagnostic`
- 产出：`RegimeQualityResult`
- 产出：`StateMapping`
- 产出：`bhattacharyya_distance(...) -> float`
- 产出：`evaluate_regime_quality(...) -> RegimeQualityResult`
- 产出：`destandardize_parameters(parameters, standardizer) -> GaussianHMMParameters`
- 产出：`build_state_cost_matrix(previous, current, policy) -> np.ndarray`
- 产出：`match_canonical_states(previous, current, policy) -> StateMapping`

- [x] **步骤 1：编写波动不同不冗余的失败测试**

构造两个收益均值相同、其余均值相同、但 covariance 明显不同的状态，断言 Bhattacharyya covariance 项为正且不会触发 `REGIME_STATES_REDUNDANT`。另构造均值、covariance、占比、持续时间和转移特征均在冻结阈值内的两个状态，断言硬拒绝。

- [x] **步骤 2：运行测试并确认质量模块缺失**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_regime_quality.RegimeQualityTest.test_equal_returns_different_volatility_are_not_redundant -v
```

- [x] **步骤 3：实现数值质量和状态诊断**

逐项实现：

```text
占比硬拒绝 < 0.03
占比警告 < 0.08
核心状态占比 >= 0.08
核心状态持续时间硬拒绝 < 3
加权平均持续时间警告 < 5
full covariance 最小特征值 < 1e-8 硬拒绝
diag covariance < 1e-8 硬拒绝
Bhattacharyya < 0.15 警告
四项冗余条件同时成立才硬拒绝
```

占比使用训练窗口 filtered probabilities 的平均值；持续时间使用 `1/(1-p_ii)`；非有限持续时间硬拒绝。

- [x] **步骤 4：编写 scaler 还原和 Hungarian 非贪心失败测试**

使用两个不同训练 scaler 表示同一 raw Gaussian，断言还原后距离接近零。使用字面成本矩阵：

```python
[[1.0, 2.0, 3.0],
 [2.0, 100.0, 4.0],
 [3.0, 4.0, 100.0]]
```

断言 Hungarian 总成本为 9，而逐行贪心会得到更差结果。再断言映射同时保留 raw 和 canonical state ID。

- [x] **步骤 5：实现全局映射**

成本固定为：

```text
0.70 * clipped Bhattacharyya
+ 0.10 * clipped absolute log duration ratio
+ 0.05 * occupancy absolute difference
+ 0.15 * transition feature L1
```

转移特征为 `(p_ii, sorted(non_self_probabilities, reverse=True))`，不依赖未知映射。调用 `scipy.optimize.linear_sum_assignment` 一次性求完整分配，禁止局部贪心。

- [x] **步骤 6：运行目标测试和完整回归**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_regime_quality -v
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest discover -s tests -v
```

- [x] **步骤 7：提交**

```bash
git add src/factor_miner/regime_quality.py tests/test_regime_quality.py
git commit -m "feat: validate and align market regimes"
```

### 任务 5：实现走步式研究比较

**文件：**
- 新增：`src/factor_miner/regime_research.py`
- 新增：`tests/test_regime_research.py`

**接口：**
- 依赖：任务 1 至任务 4 的全部领域接口。
- 产出：`MonthlyCandidateResult`
- 产出：`RegimeCandidateSummary`
- 产出：`RegimeResearchReport`
- 产出：`walk_forward_months(dates, spec, training_window) -> tuple[WalkForwardFold, ...]`
- 产出：`run_regime_research(features_by_aggregation, spec) -> RegimeResearchReport`
- 产出：`summarize_daily_diagnostics_by_regime(filtered, diagnostic_series, availability) -> RegimeConditionalSummary`

- [x] **步骤 1：编写月度切分和训练截止日失败测试**

生成跨 30 个月的合成交易日，断言每个 fold：

```text
训练截止日 < 推断月第一日
expanding 从冻结起点开始
rolling_5y 最多 1260 个完整特征日
rolling_8y 必须有完整 2016 日
```

修改任一未来月份数据不得改变历史 fold 的 scaler、参数和 filtered probability。

- [x] **步骤 2：运行测试并确认研究接口缺失**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_regime_research.RegimeResearchTest.test_monthly_folds_never_read_inference_month -v
```

- [x] **步骤 3：实现 36 配置走步执行**

每个配置固定 `K`、收益聚合、窗口和 covariance。每月顺序为：

```text
提取严格历史训练窗口
→ 拟合训练 scaler
→ 标准化训练数据
→ 多 seed 拟合
→ 训练窗口项目自有 filter
→ 质量检查
→ raw 参数还原
→ Hungarian canonical 映射
→ 冻结当月参数
→ 逐日过滤推断
→ 保存样本外 likelihood 和诊断
```

局部候选失败进入 `MonthlyCandidateResult(status="unavailable")`，不能换 `K`、窗口、covariance 或复用旧参数。

- [x] **步骤 4：编写固定排序和跨收益轨道失败测试**

构造字面候选摘要，断言每个收益轨道内部按：

```text
median_oos_log_likelihood 降序
median_mapping_cost 升序
median_min_bhattacharyya 降序
median_bic 升序
candidate_id 升序
```

选择推荐。median 与 equal-weight 分开返回推荐 ID，不用 likelihood 选跨轨道唯一冠军。少于 24 个样本外月或成功比例低于 90% 的候选不可推荐。

- [x] **步骤 5：实现概率加权条件诊断**

对满足时点合同的逐日序列计算：

```python
weighted_mean_i = sum(probability_i * value) / sum(probability_i)
```

同时输出有效日期、概率权重和、argmax 样本数、均值和标准差。日期 \(t\) 盘中已实现指标只能使用 `earliest_use_date <= t` 的状态；日期 \(t\) 收盘因子信号及从 \(t+1\) 开始的标签可以使用状态 \(t\)。

- [x] **步骤 6：运行目标测试和完整回归**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_regime_research -v
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest discover -s tests -v
```

- [x] **步骤 7：提交**

```bash
git add src/factor_miner/regime_research.py tests/test_regime_research.py
git commit -m "feat: compare walk forward regime models"
```

### 任务 6：实现固定部署与不可变状态快照

**文件：**
- 新增：`src/factor_miner/regime_snapshot.py`
- 新增：`src/factor_miner/regime_workflow.py`
- 修改：`src/factor_miner/ledger.py`
- 新增：`tests/test_regime_snapshot.py`
- 新增：`tests/test_regime_workflow.py`

**接口：**
- 依赖：任务 1 至任务 5 的全部领域接口。
- 产出：`RegimeSnapshotManifest`
- 产出：`RegimeBuildResult`
- 产出：`JsonlLedger.register_regime_research(...)`
- 产出：`JsonlLedger.register_regime_deployment(...)`
- 产出：`build_regime_snapshot(source, registered_deployment, start, end, artifact_root) -> RegimeBuildResult`
- 产出：`verify_regime_snapshot(snapshot_root: Path) -> RegimeSnapshotManifest`

- [x] **步骤 1：编写固定 `K` 和失败月份测试**

登记一个 `K=3` 部署，构造第二个月拟合失败。断言：

- 全部成功月份始终是三状态概率；
- 失败月状态为 `unavailable`；
- 不回退到 `K=2` 或 `K=4`；
- 不沿用上月参数或概率；
- raw 与 canonical ID、映射成本和质量诊断仍可追溯。

- [x] **步骤 2：运行测试并确认部署工作流缺失**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_regime_workflow.RegimeWorkflowTest.test_failed_month_never_changes_fixed_k -v
```

- [x] **步骤 3：实现部署登记和月度构建**

部署必须引用已登记研究 ID 和该研究报告中的精确候选 ID。登记前验证配置属于研究候选空间；构建时验证数据 release 与截止日。正式月内只调用项目 forward filter，且每行保存：

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

- [x] **步骤 4：编写不可变发布和篡改检测测试**

在临时 artifact root 发布合成快照，断言目录包含设计规定的七个文件，manifest 覆盖每个文件 SHA-256。重复发布相同内容得到相同 snapshot ID；修改任一概率、模型参数、部署 Spec、数据版本或 annotation 后得到不同 ID。手工篡改文件后 `verify_regime_snapshot` 必须失败。

- [x] **步骤 5：实现原子快照发布**

先写入 artifact root 内唯一 staging 目录，完成所有文件和 manifest 后原子 rename 到：

```text
artifacts/regimes/<regime_snapshot_id>
```

目标存在但内容相同则只验证并返回；内容不同硬失败。快照不包含 smoothed probability，不创建 `Evidence` 或 `Conclusion` 产物。

- [x] **步骤 6：运行目标测试和完整回归**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_regime_snapshot tests.test_regime_workflow -v
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest discover -s tests -v
```

- [x] **步骤 7：提交**

```bash
git add src/factor_miner/regime_snapshot.py src/factor_miner/regime_workflow.py \
  src/factor_miner/ledger.py tests/test_regime_snapshot.py \
  tests/test_regime_workflow.py
git commit -m "feat: publish fixed regime snapshots"
```

### 任务 7：接入正式 CLI、合成端到端测试和中文说明

**文件：**
- 修改：`src/factor_miner/cli.py`
- 修改：`tests/test_cli.py`
- 修改：`tests/test_import_boundary.py`
- 新增：`tests/test_regime_synthetic_e2e.py`
- 新增：`docs/V0.3市场状态金融流程说明.md`
- 修改：`docs/START_HERE.md`
- 修改：`README.md`

**接口：**
- 依赖：任务 1 至任务 6 的全部接口。
- 产出：`factor-miner regime research`
- 产出：`factor-miner regime register-deployment`
- 产出：`factor-miner regime build`
- 产出：`factor-miner regime verify`

- [x] **步骤 1：编写 CLI 失败测试**

用 Typer runner 和临时目录断言：

- `regime research` 读取研究 Spec 和 input-only 数据，输出研究 ID、两个收益轨道推荐和报告路径；
- `register-deployment` 拒绝未登记研究或不属于报告的候选；
- `regime build` 在 Mac 的真实模式被运行边界拒绝；
- `regime verify` 能验证合成快照并输出 provenance；
- CLI JSON 不把结果称为 Alpha、Evidence、认证状态或生产结论。

- [x] **步骤 2：运行测试并确认命令缺失**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_cli -v
```

- [x] **步骤 3：实现 `regime` Typer 子命令**

增加独立 `regime_app` 并注册到根 `app`。所有错误继续通过现有 `_fail` 输出机器可读失败码；真实 research/build 先执行服务器身份、Git、`uv.lock`、数据发布和状态表验证。`verify` 只读快照，不重新训练模型。

- [x] **步骤 4：编写合成端到端失败测试**

生成具有低波动上涨、高波动下跌和流动性冲击的三状态合成面板，运行：

```text
特征
→ 研究
→ 人工选择固定 K=3 配置
→ 部署登记
→ 月度构建
→ 快照验证
→ 概率加权条件诊断
```

断言未来数据变化不改历史 filtered probability，状态概率和为 1，日期 \(t\) 的 `earliest_use_date` 为下一交易日，快照不包含 smoothed 字段。

- [x] **步骤 5：补充导入边界和中文金融说明**

导入边界测试扫描新增核心模块，拒绝 `huan_quant`、个人 Skill、个人目录或数据库 import。中文说明先解释：

```text
市场特征代表什么
研究期选模型与正式期固定模型的区别
filtered probability 如何使用
为什么波动不同不能随便合并
状态日历能说明什么、不能说明什么
V0.4 和 V0.5 如何继续使用
```

再说明四条 CLI 命令和产物。

- [x] **步骤 6：运行完整本地验收**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest discover -s tests -v
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run factor-miner --help
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run factor-miner regime --help
git diff --check
```

- [x] **步骤 7：提交**

```bash
git add src/factor_miner/cli.py tests/test_cli.py tests/test_import_boundary.py \
  tests/test_regime_synthetic_e2e.py docs/V0.3市场状态金融流程说明.md \
  docs/START_HERE.md README.md
git commit -m "feat: complete factor miner v0.3 local workflow"
```

### 任务 8：公司 Linux 真实验收

**文件：**
- 修改：`docs/superpowers/plans/2026-07-29-factor-miner-v0.3-walk-forward-hmm-market-regime.md`
- 修改：`docs/START_HERE.md`

**接口：**
- 依赖：任务 1 至任务 7 的完整 CLI。
- 产出：服务器测试记录、真实研究报告摘要、固定部署样例和可复核快照清单。

- [x] **步骤 1：推送精确代码状态到已连接公司服务器**

先在本地确认：

```bash
git status --short
git rev-parse HEAD
```

服务器只运行与该提交一致的代码；不得把服务器数据或产物复制回 Git。

- [x] **步骤 2：在服务器安装锁定依赖并运行完整测试**

```bash
UV_CACHE_DIR=/data/factor_miner_artifacts/uv_cache uv sync --frozen
UV_CACHE_DIR=/data/factor_miner_artifacts/uv_cache uv run python -m unittest discover -s tests -v
```

预期：全部测试通过，无依赖漂移。

- [x] **步骤 3：运行真实研究比较**

使用服务器私有环境文件和独立 artifact root：

```bash
UV_CACHE_DIR=/data/factor_miner_artifacts/uv_cache uv run factor-miner regime research \
  regime_research.json \
  --env-file v0.3.env
```

检查 36 个配置的成功比例、OOS likelihood、BIC、占比、持续时间、状态距离、mapping cost 和两个收益轨道推荐。完整报告保留服务器，只在 Git 文档记录非敏感摘要和快照身份。

- [x] **步骤 4：人工固定一个部署配置并构建样例**

根据批准标准写入服务器私有 `RegimeDeploymentSpec`，依次运行：

```bash
UV_CACHE_DIR=/data/factor_miner_artifacts/uv_cache uv run factor-miner regime register-deployment \
  regime_deployment.json \
  --report-path regime_research_report.json \
  --artifact-root <独立产物根>

UV_CACHE_DIR=/data/factor_miner_artifacts/uv_cache uv run factor-miner regime build \
  --deployment-id <部署ID> \
  --start 2015-01-05 \
  --end 2026-07-27 \
  --env-file v0.3.env

UV_CACHE_DIR=/data/factor_miner_artifacts/uv_cache uv run factor-miner regime verify \
  <状态快照目录>
```

人工抽查至少三个日期的特征、训练截止日、概率和 `earliest_use_date`。若没有配置通过，保留 `REGIME_ALL_CANDIDATES_UNAVAILABLE`，不得放宽阈值重跑同一研究。

- [x] **步骤 5：记录验收边界并提交**

文档只记录：

```text
代码提交
测试数量
数据发布 ID
状态表版本
研究 ID
部署 ID
快照 ID
成功或 unavailable
非敏感聚合诊断
```

不得记录个股数据、完整概率序列、完整模型参数、私有路径解析结果或密钥。

```bash
git add docs/superpowers/plans/2026-07-29-factor-miner-v0.3-walk-forward-hmm-market-regime.md \
  docs/START_HERE.md
git commit -m "docs: record v0.3 server acceptance"
```

#### 2026-07-29 公司 Linux 验收记录

- 代码提交：`4c815317a317628836f66babe22563cbfa050f70`
- 锁定环境：`uv sync --frozen --offline` 检查 28 个包通过；Linux 完整测试 198 项通过
- 数据发布 ID：`quantlake-regime-2015-2026-v2`
- 状态表版本：`daily-security-flags-regime-v1`
- 研究 ID：`regresearch_d25def7e247ebf36f738e6f4`
- 研究结果：36 个冻结配置中 9 个达到可用门槛；15 个配置全部月份不可用
- 中位数轨道推荐：固定 `K=2`、rolling 8Y、full covariance
- 等权轨道推荐：固定 `K=3`、rolling 8Y、full covariance
- 部署 ID：`regdeploy_932be4cfbce82492fc09e596`
- 最终快照 ID：`regsnap_12b8bd1c99790513d1dd4941`
- 正式样例：2026-01-05 至 2026-07-24 共 134 个观察日，134 个 `ok`，0 个 `unavailable`
- 概率检查：逐日概率和最大误差约 `7.8e-16`；全部 `earliest_use_date` 严格晚于观察日；无 smoothed 字段
- 状态占比：`高波动下跌·弱宽度`（`canonical_state_id=0`）为 59 日，`低波动平稳·宽度中性`（`canonical_state_id=1`）为 75 日
- 非敏感经济诊断：`高波动下跌·弱宽度`相对更偏下跌、高波动、弱市场宽度；`低波动平稳·宽度中性`相对更平稳、低波动、市场宽度中性

本次验收只证明市场状态流程、时间边界、可复现性和状态经济区分度可用。尚未执行因子 RankIC、收益、回撤或换手成本的状态条件增量检验，因此快照不是交易结论，也不证明 HMM 能改善策略。
