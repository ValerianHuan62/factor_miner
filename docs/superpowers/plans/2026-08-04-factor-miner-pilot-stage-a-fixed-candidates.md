# 阶段 A：固定候选 Pilot 实施计划（Implementation Plan）

> **面向执行代理：**执行本计划时必须逐任务使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`。每一步使用复选框跟踪。

**目标：**使用一个固定假设和三个手工冻结的合法候选，打通公司 Linux 真实 QuantLake 数据、因子计算、IC、分组回测、可选 Barra、不可变运行产物和 PostgreSQL 只读投影。

**架构：**新增独立的 `pilot` 运行边界，候选输入是本地冻结的 `CandidateFactorSpec`，下游复用现有 `compiler`、`compute`、`ic_diagnostics`、`portfolio_evaluation`、`portfolio_statistics`、`portfolio_artifacts` 和 Dashboard 投影端口。Pilot 不读取、不写入 `llm_discovery_families`，不依赖 `generation seal`，也不改变 V0.5 治理模块。

**技术栈：**Python 3.11、Pydantic、Polars、Parquet、现有 typed AST 编译器、公司 Linux `/data/quantlake`、服务器产物根目录、现有 `psycopg` PostgreSQL 适配器、Streamlit 只读 Dashboard。

## 全局约束

- 所有真实候选计算、IC、回测和 Barra 读取只能在公司 Linux 服务器执行；Mac 只执行程序生成的合成测试。
- `/data/quantlake` 只读；Pilot 产物写入显式配置的独立产物根目录。
- 当前污染的 `llmfamily_1bae19965638a6ac9620e0b0` 保留为审计失败样本，不修复、不删除、不重跑、不追加事件。
- 本阶段不调用 DeepSeek，不接入 120 槽 research family，不生成或校验 generation seal，不写正式 family 账本。
- 候选结果只能称为“通过冻结可见协议计算的候选因子诊断”，不能称为有效 Alpha、认证因子或生产结论。
- 因子信号使用前一实际交易日收盘，周二开盘调仓，开盘到下一次周二开盘计算收益；禁止未来字段、未来状态和结果后调参。
- 十个多头分组组内等权，多空固定为 `Q10-Q1` 与 `Q9-Q2`，双边成本固定为 14bp，Sharpe 使用扣除成本后的净收益。
- 年化因子收益、波动和 Sharpe 使用真实交易日日历，不使用固定 252 日常量。
- 沪深300基准是计算超额收益的硬依赖；Barra 输入是可选依赖，缺失时必须发布 `not_available`，不能伪造归因。
- PostgreSQL 只投影已经发布并核验的 `run_manifest.json`；数据库失败不得回滚或删除本地产物，投影必须可重试且幂等。

---

## 现状和真实入口报告

以下内容是开始实现前已经确认的入口，不能在代码中用本地 Mac 行情替代。

| 数据用途 | 已确认的服务器入口 | 计划中的硬校验 |
| --- | --- | --- |
| 调整后日行情 | `/data/quantlake/processed/adjusted_bar/year=YYYY/data.parquet`，包含 `adj_open`、`adj_close`、`volume`、`money`、`adjust_factor`，主键为 `date, code` | 记录 release、字段、主键、复权口径和清单哈希；把内部 canonical 列明确映射为 `date, asset, open, close, volume` |
| 原始日行情 | `/data/quantlake/raw/market/daily/year=YYYY/data.parquet`，包含 `open`、`high`、`low`、`close`、`volume`、`money`，主键为 `date, code` | 只用于需要原始口径的候选或对账；不能和复权行情无记录混用 |
| 因子字段注册 | `/data/quantlake/config/rq_common_factors_field_list.csv`、`/data/quantlake/release_manifest.json`、`/data/quantlake/l2_state_manifest.json` | 运行前建立字段白名单、可得日、单位和版本哈希 |
| 状态表 | 由现有 `CompanyAShareFactorInputSource` 的显式 `state_uri` 端口读取；当前服务器目录需要在实现任务中解析到具体发布文件 | 找不到或状态版本落后于行情版本时硬失败，禁止 `fillna(0)` 或静默放宽掩码 |
| 沪深300 | 在 `/data/quantlake` 当前 README 和文件扫描中尚未找到受合同认可的 CSI300 入口 | 必须在服务器侧找到含版本、成分/权重或指数开盘价的独立来源并注册；找不到时 Pilot 不能发布超额收益结果 |
| Barra | 已发现 `/data/quantlake/raw/factors/barra_style_exposure_20220101_20260703_v2trd.parquet` 及分块目录 | 检查是否同时拥有行业暴露、Size/风格暴露、基准权重和因子收益；缺任一合同项则输出 `not_available` |
| 交易日历 | 代码合同要求 `trade_date, is_open`、版本和 SHA-256；当前文件入口尚未确认 | 必须解析服务器真实 SSE/SZSE 日历并计算哈希；周二休市按实际开放日顺延 |

实现者必须把最终解析出的 `state_uri`、CSI300 入口、Barra入口和日历入口写入 `run/input_manifest.json`。这四类入口不能通过环境变量缺失时的默认路径、Mac 文件或固定常数兜底。

## 文件结构

- 创建：`src/factor_miner/pilot_schema.py`，定义 Pilot 请求、数据身份、候选运行结果、Barra 可用性和发布状态。
- 创建：`src/factor_miner/pilot_sources.py`，只负责服务器侧 QuantLake、状态表、交易日历、CSI300 和 Barra 的输入扫描与合同核验。
- 创建：`src/factor_miner/pilot_runner.py`，编排固定候选、因子计算、IC、交易窗口、组合回测、指标和可选 Barra。
- 创建：`src/factor_miner/pilot_projection.py`，读取已发布运行并调用现有 Dashboard 投影端口；不得在这里重新计算。
- 修改：`src/factor_miner/cli.py`，增加 `factor-miner pilot run-fixed` 和 `factor-miner pilot project` 两个正式入口。
- 创建：`tests/test_pilot_sources.py`，测试入口合同、版本身份和失败语义。
- 创建：`tests/test_pilot_runner.py`，测试三个固定候选的合成纵向链路。
- 创建：`tests/test_pilot_projection.py`，测试 PostgreSQL 失败后的重投影和同一运行幂等。
- 创建：`tests/fixtures/pilot/fixed_candidates.json`，只保存合成/手工冻结的 Spec，不保存真实行情和真实运行结果。
- 修改：`docs/contracts/v1自动化研究与可视化运行合同.md`，只在阶段 A 验收后补充实际入口和命令，不提前声称真实 Pilot 已完成。

## 核心接口

```python
class PilotDataSource(Protocol):
    def inspect(self) -> PilotInputManifest: ...
    def scan_factor_inputs(self, request: FactorInputRequest) -> pl.LazyFrame: ...
    def scan_market_open(self, dates: tuple[date, ...]) -> pl.LazyFrame: ...


class PilotBenchmarkSource(Protocol):
    def inspect(self) -> BenchmarkIdentity: ...
    def scan_open_to_open(self, schedule: tuple[RebalanceWindow, ...]) -> pl.LazyFrame: ...


class PilotBarraSource(Protocol):
    def try_load(self, schedule: tuple[RebalanceWindow, ...]) -> BarraAvailability: ...


def run_fixed_pilot(
    request: PilotRunRequest,
    sources: PilotSources,
    artifact_root: Path,
) -> PilotRunResult: ...


def project_pilot_run(
    artifact_root: Path,
    pilot_run_id: str,
    store: DashboardStore,
) -> DashboardSnapshot: ...
```

`PilotRunRequest` 必须冻结：`visible_start`、`visible_end`、候选文件 SHA-256、评价政策 ID、数据发布版本、代码提交、配置哈希和产物根目录。逻辑字段名 `pilot_run_id` 复用现有 `PublishedRunManifest.run_id` 格式，即 `run_<24位十六进制>`，避免再造第二套不可变产物 ID。

固定候选文件中的三个表达式采用现有 typed AST，不使用任意 Python 代码：

```json
{
  "candidate_id": "pilot_fixed_001",
  "expression": {"op": "delta", "args": [{"op": "field", "field": "close"}], "period": 20},
  "required_fields": ["close"]
}
```

另外两个候选固定为 `rolling_mean(field("volume"), window=20, center=false)` 和 `rolling_std(field("close"), window=20, center=false)`。`close`、`volume` 必须由服务器字段注册表证明与实际输入列的映射；映射不成立时失败，不得静默改成其他字段。

## 分任务实施步骤

### 任务 1：冻结 Pilot 合同和服务器入口

**文件：**
- 创建：`src/factor_miner/pilot_schema.py`
- 创建：`tests/test_pilot_sources.py`
- 创建：`tests/fixtures/pilot/fixed_candidates.json`

**接口：**
- 输入：现有 `CandidateFactorSpec`、`PortfolioEvaluationPolicy`、`TradingCalendarIdentity`、服务器私有路径配置。
- 输出：`PilotRunRequest`、`PilotInputManifest`、`BenchmarkIdentity`、`BarraAvailability`。

- [ ] **步骤 1：写失败测试。** 测试缺少 `state_uri`、缺少交易日历、CSI300 主键重复、行情 release 不一致、字段映射不成立时分别抛出明确错误；测试候选文件恰好包含 3 个候选且每个能由 `compile_candidate` 接受。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_pilot_sources.py -v`；预期为新接口未实现或合同对象未定义的失败。
- [ ] **步骤 3：实现最小合同。** 用冻结 Pydantic 模型记录路径、版本、SHA-256、字段、截止日、复权口径、交易日历和基准身份；禁止额外字段和空字符串。
- [ ] **步骤 4：在 Linux 只读扫描真实入口。** 解析 `/data/quantlake/release_manifest.json`、调整后行情、状态表和字段注册表；解析真实交易日历和 CSI300 来源。CSI300 若仍不存在，返回 `BENCHMARK_SOURCE_UNAVAILABLE`，不得制造合成基准。
- [ ] **步骤 5：运行测试确认通过。** 运行 `pytest tests/test_pilot_sources.py -v`；预期所有合同测试通过，合成夹具不得包含任何服务器数据。
- [ ] **步骤 6：提交独立变更。** 使用 `git add src/factor_miner/pilot_schema.py tests/test_pilot_sources.py tests/fixtures/pilot/fixed_candidates.json` 后提交本任务。

### 任务 2：接通固定候选到 QuantLake 因子面板

**文件：**
- 创建：`src/factor_miner/pilot_sources.py`
- 创建：`src/factor_miner/pilot_runner.py`
- 创建：`tests/test_pilot_runner.py`

**接口：**
- 输入：`PilotRunRequest`、`PilotInputManifest`、三个 `CandidateFactorSpec`。
- 输出：每个候选一个 `signal_date, security_id, factor_value` 面板，以及包含数据身份的因子产物。

- [ ] **步骤 1：写失败测试。** 用程序生成的价格和成交量面板测试三个表达式各自按股票排序、保留 lookback、按 `valid_for_factor_compute` 掩码并产生唯一 `(signal_date, security_id)`。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_pilot_runner.py::test_fixed_candidates_compute_to_signal_panels -v`；预期因 Pilot 适配器未实现而失败。
- [ ] **步骤 3：复用现有确定性编译和计算。** 对每个候选调用 `compile_candidate(candidate, allowed_fields)`，再调用 `compute_trusted_raw_factor` 或等价的明确输入端口；把 `date, asset, raw_factor` 映射成 `signal_date, security_id, factor_value`，保留计划哈希和输入清单哈希。
- [ ] **步骤 4：建立周二信号窗口。** 使用 `build_tuesday_rebalance_schedule(calendar, visible_start, visible_end)`；信号日必须是入场日前一实际交易日，不能从未来日期回填。
- [ ] **步骤 5：运行测试确认通过。** 运行 `pytest tests/test_pilot_runner.py::test_fixed_candidates_compute_to_signal_panels -v`；预期通过并核验三份面板哈希可重复。
- [ ] **步骤 6：提交独立变更。** 提交输入适配器和因子面板链路。

### 任务 3：接通 IC、RankIC 和诊断产物

**文件：**
- 修改：`src/factor_miner/pilot_runner.py`
- 创建：`tests/test_pilot_runner.py` 中的 IC 测试

**接口：**
- 输入：信号面板、真实交易日历、open-to-open 标签生成器。
- 输出：`ic/diagnostics.json`，包含 `ic_mean`、`rank_ic_mean`、`ic_std`、`ic_ir`、`rank_ic_ir`、`ir`、`p_ic_lt_neg_002`、`p_ic_gt_pos_002`、HAC t 统计量、1/3/5/10/20 日衰减、分布、序列和自相关。

- [ ] **步骤 1：写失败测试。** 测试每个信号日仅使用该日之后的开盘价格构造标签；测试常数因子、缺失标签、重复主键和不足最小截面数时明确失败或记录该候选失败而不影响其他候选发布。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_pilot_runner.py -k ic -v`；预期新 Pilot 诊断文件尚未生成。
- [ ] **步骤 3：实现冻结标签和诊断。** 使用现有 `evaluate_ic_horizons`、`evaluate_rank_ic` 和 `FROZEN_HORIZONS=(1,3,5,10,20)`；标签以真实交易日序列生成，禁止负 shift、centered rolling、未来填充和期末成分股回填。
- [ ] **步骤 4：保存图表所需长表。** 除摘要 JSON 外保存 `daily`、`ic_sequence`、`rank_ic_sequence` 和衰减点，使 Dashboard 可绘制 IC 衰减、IC 分布、IC 序列、IC 自相关、RankIC 衰减和 RankIC 分布。
- [ ] **步骤 5：运行测试确认通过。** 运行 `pytest tests/test_pilot_runner.py -k ic -v`；预期指标和序列哈希稳定，结果好坏不改变发布状态。
- [ ] **步骤 6：提交独立变更。** 提交 IC 标签和诊断产物。

### 任务 4：接通十组、两种多空和 open-to-open 回测

**文件：**
- 修改：`src/factor_miner/pilot_runner.py`
- 修改：`src/factor_miner/portfolio_data_source.py`（仅补充 QuantLake canonical 列适配，不改变回测算法）
- 创建：`tests/test_pilot_runner.py` 中的组合测试

**接口：**
- 输入：因子面板、服务器开盘价、`RebalanceWindow`、`PortfolioEvaluationPolicy`、CSI300 open-to-open 收益。
- 输出：`portfolio/daily.json`、`portfolio/weights.json`、`portfolio/metrics.json`。

- [ ] **步骤 1：写失败测试。** 测试周二调仓、周二休市顺延、每组等权、Q10-Q1、Q9-Q2、首次建仓换手、双边 14bp、净 Sharpe 和开盘到开盘收益。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_pilot_runner.py -k portfolio -v`；预期固定候选尚未生成完整组合产物。
- [ ] **步骤 3：实现数据对齐。** 调用 `align_open_to_open_panel` 生成唯一资产收益，严格核验 `entry_open > 0`、`exit_open > 0` 和主键唯一；调用 `run_portfolio_backtest`，政策固定为现有 `PortfolioEvaluationPolicy()`。
- [ ] **步骤 4：实现基准和成本对账。** CSI300 源输出唯一 `exit_date, benchmark_return`；调用 `calculate_portfolio_metrics`，用真实日历计算年化；成本公式固定为现有合同定义的 `turnover × 14 / 10000`，净收益先扣成本再计算 Sharpe。
- [ ] **步骤 5：保存分组表和图形输入。** 为 Q1 至 Q10、Q10-Q1、Q9-Q2 保存毛收益、净收益、换手、成本、累计收益、超额收益和回撤序列；保存 10 分组收益表和 10 分组超额收益图所需的完整序列。
- [ ] **步骤 6：运行测试确认通过。** 运行 `pytest tests/test_pilot_runner.py -k portfolio -v`；预期合同测试通过，即使合成因子刻意表现差也能返回完整结果。
- [ ] **步骤 7：提交独立变更。** 提交组合数据适配和回测编排。

### 任务 5：加入不阻塞的 Barra 归因

**文件：**
- 修改：`src/factor_miner/pilot_runner.py`
- 创建：`tests/test_pilot_runner.py` 中的 Barra 测试

**接口：**
- 输入：组合权重、CSI300 基准权重、行业/Size/风格暴露、因子收益、`BarraInputIdentity`。
- 输出：`barra/attribution.json`，状态为可用时包含暴露和已实现贡献；不可用时包含 `status="not_available"`、缺失输入和数据身份。

- [ ] **步骤 1：写失败测试。** 测试完整 Barra 输入调用现有 `calculate_barra_attribution`；测试缺行业字段、缺基准权重、缺因子收益或身份不一致时不抛出 Pilot 总失败，而是产生 `not_available`。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_pilot_runner.py -k barra -v`；预期可选归因状态尚未接入。
- [ ] **步骤 3：实现可选适配器。** 读取已确认的 `/data/quantlake/raw/factors/barra_style_exposure_20220101_20260703_v2trd.parquet`，仅在行业暴露、Size/风格暴露、基准权重、因子收益和身份全部满足合同后运行；只具备风格文件时记录具体缺失项。
- [ ] **步骤 4：运行测试确认通过。** 运行 `pytest tests/test_pilot_runner.py -k barra -v`；缺 Barra 不阻塞 IC、回测和发布，存在数据时对账误差满足现有政策。
- [ ] **步骤 5：提交独立变更。** 提交可选 Barra 端口。

### 任务 6：发布不可变 Pilot 运行产物

**文件：**
- 修改：`src/factor_miner/pilot_runner.py`
- 修改：`src/factor_miner/portfolio_artifacts.py`（只在现有发布合同不足以承载 Pilot 输入清单时增加字段，不改变已有哈希规则）
- 创建：`tests/test_pilot_artifacts.py`

**接口：**
- 输入：三个候选的全部计算结果、数据身份、政策、Barra 状态和代码/配置哈希。
- 输出：`artifacts/runs/{pilot_run_id}/run_manifest.json` 及其引用文件。

- [ ] **步骤 1：写失败测试。** 测试同一 `pilot_run_id` 重复发布得到相同清单；同一 ID 内容不同则硬失败；缺少一个候选结果、基准结果或输入身份不能发布；三个候选均差时仍然发布。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_pilot_artifacts.py -v`；预期 Pilot 清单编排尚未实现。
- [ ] **步骤 3：计算不可变身份。** 对排序后的候选哈希、输入清单哈希、政策 ID、可见区间和代码提交做规范 JSON 哈希，生成 `run_<24位十六进制>`；不得用时间戳随机生成会破坏幂等的身份。
- [ ] **步骤 4：原子写入并核验。** 先写临时目录，再用现有 `publish_run_artifacts` 写入 `run/metrics.json`、`run/input_manifest.json`、候选 Spec、`ic/diagnostics.json`、`portfolio/daily.json`、`portfolio/metrics.json`、`barra/attribution.json` 和可视化数据，最后写 `run_manifest.json`。
- [ ] **步骤 5：运行测试确认通过。** 运行 `pytest tests/test_pilot_artifacts.py -v`；预期清单哈希、引用哈希和不可变行为全部通过。
- [ ] **步骤 6：提交独立变更。** 提交发布器编排。

### 任务 7：实现 PostgreSQL 幂等投影和 CLI

**文件：**
- 创建：`src/factor_miner/pilot_projection.py`
- 修改：`src/factor_miner/cli.py`
- 修改：`dashboard/pg_store.py`（仅在现有 Schema 缺少 Pilot 字段时补充幂等约束）
- 创建：`tests/test_pilot_projection.py`

**接口：**
- 命令：`factor-miner pilot run-fixed --candidates ... --artifact-root ...`。
- 命令：`factor-miner pilot project <pilot_run_id> --artifact-root ... --dsn ...`。
- 投影函数：`project_pilot_run(artifact_root, pilot_run_id, store)`。

- [ ] **步骤 1：写失败测试。** 测试投影先调用 `verify_published_run`；PostgreSQL 连接失败时本地产物仍可被核验；同一运行重复投影不会产生不同快照；不同快照复用同一 ID 时硬失败。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_pilot_projection.py -v`；预期 CLI 和 Pilot 投影入口尚未实现。
- [ ] **步骤 3：实现投影。** 只从 `run_manifest.json` 和引用 JSON 读取，调用现有 `project_run_artifacts` / `PostgresDashboardStore.replace_run_snapshot`；数据库不参与因子计算、指标计算或发布判定。
- [ ] **步骤 4：实现 CLI。** `run-fixed` 在完成本地产物后再尝试投影；投影失败返回非零状态并打印重试命令，不删除本地产物。`project` 只允许已有 published run。
- [ ] **步骤 5：运行测试确认通过。** 运行 `pytest tests/test_pilot_projection.py -v` 和 `pytest tests/test_v1_synthetic_e2e.py -v`。
- [ ] **步骤 6：提交独立变更。** 提交 CLI 和投影层。

### 任务 8：Linux 真实 Pilot 验收

**文件：**
- 修改：`docs/contracts/v1自动化研究与可视化运行合同.md`
- 创建：`docs/runbooks/阶段A固定候选Pilot真实运行.md`

- [ ] **步骤 1：在服务器建立新的 `pilot_run_id`。** 不使用污染 family ID，不向旧 `llm_events.jsonl` 写入任何事件。
- [ ] **步骤 2：执行真实入口预检。** 记录 QuantLake release、状态表、日历、CSI300、Barra 可用性、代码提交和配置哈希。
- [ ] **步骤 3：执行 `factor-miner pilot run-fixed`。** 真实运行只写独立产物根目录；候选表现差不能改变 `published` 判定。
- [ ] **步骤 4：核验本地产物。** 执行 `verify_published_run`，检查所有引用哈希、三候选、IC、十组、多空、净成本、超额收益和 Barra 状态。
- [ ] **步骤 5：执行 PostgreSQL 投影。** 连接失败时保留发布成功状态，恢复连接后单独执行 `factor-miner pilot project`。
- [ ] **步骤 6：打开只读 Dashboard。** 页面只能读取投影，展示批次身份、分组收益表、两种多空、超额收益、IC 诊断和 Barra `not_available` 或归因结果。
- [ ] **步骤 7：完成验收记录。** 只有以下条件全部成立，才把阶段 A 标记完成：真实运行有 `run_manifest.json`，数据库可重投影，Dashboard 可读取，三个候选无论好坏都留有完整诊断，且旧 family 账本字节未变化。

## 阶段 A 明确验收标准

1. Linux 真实运行可用全新的 `pilot_run_id` 完整走完固定三候选链路，不调用 DeepSeek，不读取或写入 120 槽和旧 family 账本。
2. 每个候选有冻结 Spec、AST 哈希、编译计划、输入数据身份、因子面板、IC/RankIC 全部诊断、十组收益、Q10-Q1、Q9-Q2、净成本、真实日历年化指标和 CSI300 超额收益。
3. 组合确实是周二开盘调仓、open-to-open、组内等权、双边 14bp；Sharpe 来自净收益。
4. Barra 输入可用时输出行业、Size/风格暴露和已实现归因；不可用时输出 `not_available`，但不阻塞前述结果发布。
5. 产物先于数据库发布；数据库故障不破坏产物，重试投影结果与首次一致。
6. 指标好坏不决定是否发布；发布判断只依据输入合同、计算完整性和哈希核验。
7. 未找到受认可 CSI300 源时，运行必须明确失败为基准输入合同失败，不能发布虚假的超额收益。

阶段 A 完成后，才允许执行阶段 B；在此之前不做 DeepSeek 接入、generation seal 或 120 槽 campaign。
