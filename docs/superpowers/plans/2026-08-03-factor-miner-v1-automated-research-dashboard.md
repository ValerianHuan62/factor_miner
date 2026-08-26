# Factor Miner V1 自动化研究与可视化实施计划

> **给实施代理：** 实施本计划时使用 `superpowers:executing-plans`，按任务逐项执行。每一步使用复选框跟踪，每个任务独立测试并单独提交。这里的自动化运行角色不是开发代理，不能获得任意文件、终端、数据库或 Python 执行权限。

**目标：** 在 V0.5 受控 LLM 候选生成完成后，建立一条“人工批准假设 → 自动生成候选 → 服务器周二开盘 open-to-open 组合回测 → Barra 暴露与收益归因 → 只读 Dashboard”的可恢复研究流水线。

**架构：** V1 保留 V0.5 的假设闸门、白名单 typed AST、内容哈希和结果前封存规则，并沿用四条生成路线的 120 槽研究族：每条路线 10 个假设、每个假设最多 3 个因子设计。新增组合评价、风险归因和展示层；PostgreSQL 只作为由不可变文件产物投影出的 Dashboard 读模型，不能替代 JSON/JSONL 账本、候选 Spec、Campaign Spec 或服务器 Parquet 产物。所有真实计算仍在公司 Linux 服务器，Mac 只运行合成测试。

**技术栈：** Python 3.12、Pydantic 2、Polars、statsmodels、Typer、Parquet、规范 JSON/JSONL、PostgreSQL 读模型、`psycopg`、Streamlit、Plotly。新增依赖必须先完成官方来源、版本、维护状态和许可证记录，再写入 `pyproject.toml` 与 `uv.lock`。

## 本轮执行记录（2026-08-03）

- 已完成服务器端 LLM 隔离运行器、`server-run-approved` 入口和 V0.5 外发运行合同；Mac 只返回哈希摘要，不能执行真实 DeepSeek。
- 已完成 120 槽口径的组合/Barra 冻结政策、周二休市顺延、open-to-open 数据对齐、十组等权、Q10−Q1、Q9−Q2、14bp 成本、实际交易日日历年化、IC/RankIC 诊断、Barra 已实现归因和不可变产物发布。
- 已完成 Dashboard 只读投影合同、初始 PostgreSQL DDL 和合成内存读模型；公司服务器已创建独立数据库并完成迁移，Streamlit 上线尚未执行。
- 已完成批准假设后的表达式编排：每个批准假设固定 `C001~C003`，每条 LLM arm 最多 10 个假设；表达式硬闸门、semantic lint、候选登记、恢复和请求正文防火墙均已接入。
- 已新增服务器固定程序 `family-seal-auto` 和 `shadow-run`：从本地 120 个槽终态对象重算 hash，并按固定变异/grammar 规则生成两个 shadow arm；仍需完整批准批次和真实数据端口后才能封存并评价。
- 已完成组合/IC/Barra 发布 workflow、PostgreSQL 参数化投影入口和五个只读页面骨架；截至本轮合成回归为 349 个测试通过。Codex 未读取 QuantLake 原始数据，也未代发外部 DeepSeek 请求。

## 任务完成状态

| 任务 | 状态 | 已完成内容 |
|---|---|---|
| 前置任务 0 | 已完成 | Linux 服务器端 LLM 隔离运行器、精确授权检查、失败摘要和 CLI 入口 |
| 任务 1 | 已完成 | 周二调仓、open-to-open、十组等权、14bp、沪深300、Barra 政策合同 |
| 任务 2 | 已完成 | 真实交易日历、周二休市顺延、信号/入场/退出日期对齐、开盘价主键校验 |
| 任务 3 | 已完成 | Q1~Q10、Q10−Q1、Q9−Q2、换手和毛/净收益计算 |
| 任务 4 | 已完成 | 实际交易日日历年化、净 Sharpe、最大回撤、超额收益和信息比率 |
| 任务 5 | 已完成 | IC/RankIC、1/3/5/10/20日衰减、分布、自相关、HAC t 值和阈值概率 |
| 任务 6 | 已完成 | 行业/Size 主动暴露、已实现 Barra 因子贡献和特异残差对账；完整风险分解仍需协方差/特异风险输入 |
| 任务 7 | 已完成 | 不可变组合/IC/Barra 产物发布、manifest 哈希核验、成功/失败/中断终态事件已接入 workflow |
| 任务 8 | 部分完成 | LLM 表达式批次、硬闸门、semantic lint、登记、恢复、单对象私有配置兼容、两个 deterministic shadow arm 和自动 seal 入口已完成；两条 LLM arm 的完整批准批次仍未跑完 |
| 任务 9 | 部分完成 | Dashboard 投影合同、DDL、参数化 PostgreSQL 适配器、重建测试和服务器投影 CLI 已完成；服务器已启动 PostgreSQL 16、创建独立库并执行 8 张表迁移，实际投影仍待依赖验收 |
| 任务 10 | 部分完成 | Streamlit 只读五页和指标图已完成；服务器私有 DSN 已建立，Streamlit/Plotly 依赖与只读服务尚未验收 |
| 任务 11 | 未开始 | 公司 Linux 真实数据端到端验收未执行 |

所以不是“一个 task 都没完成”，而是计划里的复选框没有同步成可读的状态表，造成了误解。当前最接近用户目标的未完成主链是：完整批准批次 → 120 槽封存 → Linux 回测数据端口 → 质量闸门 → 只投影通过检验因子的聚合结果；确定性 shadow arm 已不再是阻塞项。

## Mac PostgreSQL 方案的边界

Mac PostgreSQL 可以单独建立一个 Dashboard 数据库，只保存通过检验因子的展示元数据和聚合结果，例如候选 ID、Spec 哈希、因子分类、公式摘要、回测区间、净 Sharpe、年化收益、最大回撤、IC/RankIC 摘要和 Barra 状态。原始行情、逐股票面板、完整逐日结果、失败槽位账本、密钥和服务器私有配置不进入该数据库。

建议把它定义为可删除、可重建的 `factor_miner_dashboard` 读库；Linux 仍是唯一真实计算端，Mac PostgreSQL 只是“通过检验因子目录”。正式跨机器导出前仍需确认公司的数据外发政策；若不允许导出，Dashboard 继续部署在服务器，Mac 只通过 SSH 隧道访问。

## 金融研究边界

- V1 的结果只能称为“通过可见验证的候选因子”及其组合和风险诊断，不能称为认证因子、有效 Alpha 或生产结论。
- LLM 只生成结构化假设和白名单 AST；它不能读取结果、修改评价政策、动态补抽、自动改变参数或编写任意 Python。
- 结果生成前冻结图谱身份、每条路线 10 个假设槽位、120 个候选槽位、评价政策、组合政策、Barra 政策、提示身份和数据身份。
- 失败、重复、空缺、中断和基础设施失败继续占用预登记槽位，不得根据结果补抽或新建 Campaign 重置多重检验。
- 周二调仓信号使用前一交易日收盘可得信息，成交使用周二开盘；收益使用本次调仓开盘到下一次调仓开盘的 open-to-open 区间。
- 真实年化系数来自版本化交易日日历，禁止把 252 写成不可覆盖的常量。
- 10 个多头分组组内等权；Q10−Q1 和 Q9−Q2 的方向、敞口、成本和基准口径全部写入不可变政策。
- Barra 暴露和因子收益只能在时点正确的数据合同通过后使用；没有协方差矩阵或个股特异风险时，只发布已实现暴露/收益归因，不伪造完整风险分解。
- Dashboard 是只读展示层，不改变候选状态、评价参数、账本事件或研究预算。
- 服务器位于内网不等于可以放宽外部模型外发规则；DeepSeek 仍只接收通过公司政策、敏感扫描和精确哈希授权的脱敏请求。
- Codex 不读取 QuantLake 原始数据进入模型上下文；服务器上的固定编排器直接读取数据、生成脱敏 brief、调用 DeepSeek、保存响应和运行评价，Codex 只接收状态、ID、哈希和失败码。

## V1 完成后的运行链路

```text
V0.4 冻结覆盖图谱
  → V0.5 生成 10 个假设槽
  → 人工批准假设
  → V1 自动调用表达式代理
  → 白名单 AST、单位、availability、lookback、未来依赖、重复和编译检查
  → 候选登记与 120 槽研究族 generation seal
  → 公司 Linux 服务器计算原始因子和逐日 IC/RankIC
  → 每周二开盘按因子分成 Q1~Q10
  → open-to-open 分组、Q10−Q1、Q9−Q2 收益与双边成本
  → 沪深300超额收益、HAC/Bonferroni、IC诊断
  → Barra 行业、Size、风格暴露与收益归因
  → 原子发布 JSON/JSONL/Parquet
  → 投影 PostgreSQL 读模型
  → Streamlit 只读 Dashboard
```

## 服务器数据边界与产物布局

`/data/quantlake` 继续作为只读上游，不能写入、复制到 Git 或发送给外部模型。真实运行产物写入与 QuantLake 同级的独立目录：

```text
/data/factor_miner_artifacts/
├── state/
│   ├── candidates/{candidate_id}.json
│   ├── campaigns/{campaign_id}.json
│   ├── policies/{policy_id}.json
│   ├── families/{family_id}.json
│   ├── reference_libraries/{library_id}.json
│   ├── llm_discovery_families/{family_id}/
│   └── ledger/trials.jsonl
├── artifacts/
│   └── runs/{run_id}/
│       ├── run_manifest.json
│       ├── candidate_index.json
│       ├── candidates/{candidate_id}/
│       │   ├── compiled_plan.json
│       │   ├── raw_factor.parquet
│       │   ├── daily_ic.parquet
│       │   ├── daily_rank_ic.parquet
│       │   ├── ic_decay.parquet
│       │   ├── portfolio_daily.parquet
│       │   ├── group_returns.parquet
│       │   ├── barra_exposure.parquet
│       │   ├── barra_attribution.parquet
│       │   ├── metrics.json
│       │   └── charts/
│       └── dashboard_index.json
└── dashboard/
    ├── postgres.env
    └── migrations/
```

JSON 保存不可变对象和小型摘要；JSONL 保存追加式状态事件；Parquet 保存逐日、逐组合和逐暴露长表；Dashboard 不复制原始 QuantLake 数据。不得创建名为 `Evidence` 或 `Conclusion` 的产物目录或文件。

PostgreSQL 只保存 Dashboard 读模型：候选身份、运行身份、摘要指标、状态、产物引用和可查询的低维时间序列。每一行都绑定 `run_id`、`candidate_id`、`run_manifest_sha256` 和 `source_artifact_sha256`。删除 PostgreSQL 后，必须可以从服务器不可变产物重新投影出相同读模型。

## 冻结的 V1 组合政策

```text
policy_version = portfolio-v1
rebalance_weekday = Tuesday
signal_observation = previous_trading_day.close
entry = scheduled_rebalance.open
exit = next_scheduled_rebalance.open
return_convention = open_to_open
group_count = 10
group_weighting = equal_weight_within_group
long_short = Q10_minus_Q1, Q9_minus_Q2
benchmark = CSI300
round_trip_cost_bps = 14
annualization = versioned_trading_calendar
```

### 调仓日与收益区间

- 从版本化交易日历选出每周二；如果周二休市，使用该周二之后的第一个交易日，并在运行摘要中记录顺延原因。
- 调仓信号日期是调仓交易日的前一个实际交易日。任何使用调仓日收盘或之后字段的表达式都必须失败。
- 因子在信号日期收盘后形成，组合在调仓交易日开盘执行。
- 当期收益为 `open(exit_date) / open(entry_date) - 1`，不使用收盘价替代开盘价。
- 首版不做盘中成交模拟；开盘成交价、可交易状态、停牌、涨跌停和缺失价格由服务器数据合同处理。

### 分组、权重与方向

- 每个调仓日先应用冻结股票池和可交易状态，再按因子值形成十个横截面分组。
- 分组使用稳定排名和固定并列处理；有效股票不足、因子值为空或开盘价为空时不能用 `fillna(0)` 伪造可交易结果。
- 组内等权，权重总和为 1；组内无有效股票时该组该期为不可用并记录原因。
- 根据候选的 `expected_sign` 生成方向规范化排序列，使 Dashboard 中 Q10 始终表示事前预期的正向一端，同时保留原始因子方向。
- 额外市值加权只作为敏感性诊断，不改变主评价和研究槽位。

### 多空组合、成本与超额收益

- `Q10−Q1` 的毛收益为 Q10 组合收益减 Q1 组合收益；`Q9−Q2` 同理。
- 多空组合使用两条等权腿分别计算换手和成本，并记录总敞口为 2.0；不能把多空收益当作零成本单腿收益。
- 对任意组合定义 `one_way_turnover = 0.5 * sum(abs(new_weight - old_weight))`。
- 双边总成本为 `one_way_turnover * 0.0014`；组合净收益为毛收益减成本。
- 因子收益同时保存回测期累计毛收益、回测期累计净收益和年化净收益，避免“因子收益”与“年化收益”混为一个字段。
- 沪深300超额收益按相同交易日计算相对财富；Dashboard 同时展示基准收益、组合收益和相对基准收益。
- Sharpe 只使用扣除成本后的日收益；信息比率必须明确使用相对沪深300的净超额序列。
- 最大回撤基于净收益财富曲线；多空组合必须绑定固定总敞口和初始财富约定后才允许计算。

### 真实交易日日历年化

- `TradingCalendar` 提供每个交易日的顺序号、年份和开收盘可用性。
- 年化系数从实际日历中计算并写入政策快照，不使用固定 252。
- 年化收益使用净财富的几何年化；Sharpe 使用同一日历年化系数的平方根。
- 跨年区间、首尾不完整年份和节假日顺延都必须有合成测试。

## 冻结的 Barra 归因政策

### 最小数据合同

完整 Barra 风险归因需要以下时点正确数据：

```text
exposure_matrix(asset, factor, asof_date)
factor_returns(factor, holding_interval)
benchmark_weights(asset, asof_date)
factor_covariance(factor_a, factor_b, asof_date)
specific_risk(asset, asof_date)
```

若只有前 3 类数据，可以计算暴露监控和已实现收益归因；若缺少协方差或特异风险，`full_risk_decomposition` 必须硬失败，不能用单位矩阵、历史波动率或零特异风险静默代替。

### 暴露与归因定义

对组合 (p)、因子 (k)、调仓日 (t)：

```text
portfolio_exposure[p,k,t] = Σ_i weight[p,i,t] * exposure[i,k,t]
active_exposure[p,k,t] = portfolio_exposure[p,k,t] - benchmark_exposure[k,t]
realized_factor_contribution[p,k,t] = active_exposure[p,k,t] * factor_return[k,t→t+1]
specific_residual[p,t] = active_return[p,t] - Σ_k realized_factor_contribution[p,k,t]
```

完整风险分解使用：

```text
factor_risk = active_exposure.T @ factor_covariance @ active_exposure
specific_risk = Σ_i active_weight[i]^2 * specific_variance[i]
total_risk = factor_risk + specific_risk
```

Dashboard 至少显示：

- 行业绝对暴露和主动暴露 Top N；
- Size、市值、Value、Momentum、Volatility、Liquidity 等风格暴露；
- 暴露随调仓日期的变化；
- 已实现行业/风格收益贡献；
- 解释收益、特异残差和总收益的对账；
- 有协方差时显示因子风险、特异风险和总风险；
- 没有协方差时显示 `realized_attribution_only`，禁止显示完整风险百分比。

Barra 暴露必须使用信号日期或该数据合同规定的最近可得日期，不能使用调仓成交之后的暴露。行业字段、Size 字段、协方差版本和特异风险版本均记录在 `barra_manifest.json` 和运行清单中。

## V1 研究状态机

```text
human_approved_hypothesis
  → expression_requested
  → expression_recorded
  → candidate_structurally_validated
  → candidate_registered
  → generation_sealed
  → raw_factor_computed
  → ic_evaluated
  → portfolio_evaluated
  → barra_attributed
  → artifacts_published
  → dashboard_projected
```

任何阶段失败都转为带稳定错误码的终态，并追加 `supersedes_event_id` 纠错事件。`portfolio_evaluated` 不能跳过 `generation_sealed`、数据来源检查、未来依赖探针、原始因子质量检查、RankIC 闸门或必要的结果前校验。

人工批准后不得再暂停到逐个公式复制环节。每条生成路线一个批次最多处理 10 个批准假设；每个批准假设最多 3 个候选表达式，四条路线合计 120 个候选槽位。个人试运行使用一次性 `LLMCampaignScopeAuthorization`，绑定研究族、角色、脱敏信息类别、假设数量、调用次数和有效期；固定程序仍逐请求扫描、录制请求哈希并拒绝越界内容。所有 DeepSeek 请求顺序执行并逐字录制；账本保持单写入器。任意中断都由 `resume` 根据最后一个不可变事件继续，不重复调用已成功录制的请求。

### 假设批准与外发授权必须分开

人工批准假设表示“允许继续研究这组事前主张”。个人试运行另需一次绑定研究族的范围授权，但不再要求对表达式和 semantic lint 的每个精确请求哈希分别点击；固定程序会在每次调用前检查 payload、角色、信息类别、模型、预算、槽位和调用次数。任何越界内容仍然硬失败；高风险运行仍可使用内容寻址的 `ExportAuthorizationBundle` 精确授权。

若公司部署的是公司边界内模型，则可以使用等价的内部 `ModelExecutionPolicy` 替代外发授权；仍然必须逐字录制请求和响应，并保留相同的状态转换和结果前封存规则。

### 外部模型的无数据运行模式

V1 提供两个外部 DeepSeek 模式，二者都不把 QuantLake 原始数据交给 Codex：

1. `public_capability_only`：只发送公共经济学问题、脱离公司结果的批准假设、公开字段别名、白名单算子、窗口能力和 AST Schema。表达式代理在这个模式下不需要任何公司数据；这是默认模式。
2. `sanitized_coverage_brief`：服务器固定程序从覆盖图谱生成只含公共别名、分箱数量、失败类型和等级标签的 brief，经过敏感扫描、公司外发政策和精确哈希授权后发送。完整图谱、真实因子 ID、个股、IC 序列和相关矩阵永不外发。

如果没有有效的公司外发政策，`sanitized_coverage_brief` 必须硬失败，但 `public_capability_only` 仍可用于生成通用假设和因子设计。若研究必须利用公司私有覆盖结果，则使用公司边界内模型或公司批准的内部代理，不通过外部 DeepSeek 绕过政策。

LLM 永远不能看到当前批次的 IC、分组收益、Sharpe、回撤或 Barra 结果；结果只供 Dashboard 和后续人工研究读取。下一批若要使用历史结果，必须新建并冻结新的发现上下文和研究族身份。

## V0.5 真实试运行的解锁方式

当前 V0.5 卡住的根因不是服务器在内网，而是如果由 Codex 读取 QuantLake 内容、拼接提示词并把原始内容交给外部模型，就会越过项目的数据边界。正确的解法是把编排器放到公司 Linux 上：

```text
Codex
  → 只发送命令、批准动作和私有文件路径
服务器固定程序
  → 读取 QuantLake
  → 构造 public_capability_only 或 sanitized_coverage_brief
  → 在服务器完成敏感扫描和政策检查
  → 从服务器读取 DEEPSEEK_API_KEY
  → 调用 DeepSeek
  → 在服务器保存请求、响应、候选和结果
Codex
  ← 只返回 run_id、状态、哈希、槽位终态和失败码
```

服务器进程的标准输出不能打印 prompt 正文、响应正文、个股代码、行情值、完整 IC、完整 Barra 暴露或数据库连接串。响应 JSON 和原始结果留在服务器私有产物根目录，Dashboard 也只从服务器读模型读取。

这条路径可以完成当前 V0.5 的 10 个假设试运行；V1 继续沿用原设计的每个假设 3 个表达式、每条路线 30 个槽位、四条路线合计 120 个槽位。这样可以在不改变原有多重检验族和比较基线的前提下，继续增加自动回测、Barra 归因、产物发布和 Dashboard。若未来要扩展到每个假设 10 个设计，必须另建版本化设计和新的研究族，不能在 120 槽账本中途扩容。

## Dashboard 设计

V1 Dashboard 使用 Streamlit，只读绑定服务器产物和 PostgreSQL 读模型。默认监听 `127.0.0.1`，通过 SSH 隧道访问；若确需局域网访问，必须增加身份认证、IP 白名单和只读数据库账号。

### 页面一：批次总览

展示每批 10 个假设及其最多 30 个候选；四条生成路线合计最多 120 个候选：

- 候选 ID、假设摘要、状态、方向和回看长度；
- RankIC 均值、ICIR、HAC t 值、校正 p 值和覆盖率；
- 回测期净收益、年化收益、Sharpe、最大回撤和换手率；
- Q10−Q1、Q9−Q2 的净收益和风险；
- Barra 归因状态、最大主动行业暴露和最大主动 Size 暴露；
- 失败原因、数据版本和运行 ID。

### 页面二：IC 诊断

- IC/RankIC 均值、标准差、IR、HAC 推断和 Bonferroni 状态；
- `P(IC < -0.02)`、`P(IC > 0.02)`；
- 日序列、分布、自相关和年度摘要；
- `1/3/5/10/20` 日衰减曲线。

衰减期限必须在结果揭晓前冻结；除主评价期限外的期限只作为描述性诊断，不能用图形结果临时选择最优期限。

### 页面三：分组回测

表格固定包含：

```text
分组、回测期收益、年化收益、超额收益、超额年化收益、最大回撤、
超额最大回撤、年化波动、超额年化波动、换手率、净Sharpe、信息比率
```

同时展示 Q1～Q10 累计净值、Q10−Q1、Q9−Q2、沪深300和相对基准曲线。页面必须区分毛收益和净收益，不能只展示扣成本后的一个数字。

### 页面四：Barra 风险归因

- 行业暴露热力图和时间序列；
- Size 与风格暴露时间序列；
- 主动暴露 Top N；
- 已实现因子贡献堆叠图；
- 组合收益、解释收益、特异残差对账图；
- 完整风险模型缺失时显示明确的数据缺口，而不是空白图或零值。

### 页面五：审计与运行

- `run_id`、candidate/family/campaign ID；
- CandidateSpec、Policy、Barra manifest、数据 release、交易日历、代码提交和配置哈希；
- 每个槽位的生成终态和失败原因；
- 每个 Parquet 和 JSON 产物的 SHA-256；
- Dashboard 读取时间和读模型版本。

Dashboard 不提供“重新回测”“修改参数”“批准候选”“删除运行”按钮。

## 任务拆分

### 前置任务 0：建立服务器端 LLM 隔离运行器

**文件：**

- 新建：`src/factor_miner/llm_server_runner.py`
- 修改：`src/factor_miner/cli.py`
- 新建：`tests/test_llm_server_runner.py`
- 新建：`docs/contracts/v0.5服务器端外发运行合同.md`

**接口：**

```python
class ServerRunSummary(BaseModel):
    run_id: str
    status: str
    request_hashes: tuple[str, ...]
    terminal_slot_count: int
    artifact_manifest_sha256: str

def run_server_side_llm_pipeline(
    family_id: str,
    mode: Literal["public_capability_only", "sanitized_coverage_brief"],
    artifact_root: Path,
) -> ServerRunSummary:
    pass
```

- [ ] 写失败测试：Mac 运行真实模式失败；服务器模式的 stdout 不包含请求正文、响应正文、个股代码、行情值、完整 IC、密钥或数据库 URL；无公司外发政策时 `sanitized_coverage_brief` 失败；`public_capability_only` 不读取 QuantLake 内容；失败请求仍保存哈希和终态。
- [ ] 运行：`uv run python -m unittest tests.test_llm_server_runner -v`，确认失败。
- [ ] 实现 `factor-miner llm server-run-approved`，使 DeepSeek HTTP、响应校验、候选转换和状态事件全部在服务器进程内完成；CLI 只输出 `ServerRunSummary`。
- [ ] 运行同一命令，确认通过。
- [ ] 单独提交：`feat: run llm pipeline inside server boundary`。

### 任务 1：冻结 V1 政策对象

**文件：**

- 新建：`src/factor_miner/portfolio_schema.py`
- 新建：`src/factor_miner/barra_schema.py`
- 修改：`src/factor_miner/errors.py`
- 新建：`tests/test_portfolio_schema.py`
- 新建：`tests/test_barra_schema.py`

**接口：**

```python
class PortfolioEvaluationPolicy(BaseModel):
    pass

class BarraEvaluationPolicy(BaseModel):
    pass

class TradingCalendarIdentity(BaseModel):
    pass

def portfolio_policy_id(policy: PortfolioEvaluationPolicy) -> str:
    pass

def barra_policy_id(policy: BarraEvaluationPolicy) -> str:
    pass
```

- [ ] 写失败测试：拒绝非 Tuesday 调仓、非 `open_to_open`、非 10 组、非等权主组合、非 14bp 双边成本、缺失沪深300、固定 252 年化、可变多空定义和候选覆盖评价政策。
- [ ] 运行：`uv run python -m unittest tests.test_portfolio_schema tests.test_barra_schema -v`，确认测试因类型和政策对象不存在而失败。
- [ ] 实现冻结 Pydantic 对象，所有字段 `extra="forbid"`、不可变并计算内容哈希。政策中保存成本、分组、调仓、基准、日历身份、Barra 模型身份和是否允许仅已实现归因。
- [ ] 运行同一命令，确认通过。
- [ ] 单独提交：`feat: freeze v1 portfolio and barra policies`。

### 任务 2：实现周二交易日历和 open-to-open 对齐

**文件：**

- 新建：`src/factor_miner/trading_schedule.py`
- 新建：`src/factor_miner/portfolio_data_source.py`
- 修改：`src/factor_miner/company_a_share.py`
- 新建：`tests/test_trading_schedule.py`
- 新建：`tests/test_portfolio_data_source.py`

**接口：**

```python
class RebalanceWindow(BaseModel):
    signal_date: date
    entry_date: date
    exit_date: date

def build_tuesday_rebalance_schedule(
    calendar: pl.DataFrame,
    visible_start: date,
    visible_end: date,
) -> tuple[RebalanceWindow, ...]: ...

def align_open_to_open_panel(
    factor_panel: pl.LazyFrame,
    market_panel: pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
) -> pl.LazyFrame: ...
```

- [ ] 写失败测试：周二休市顺延、信号只能来自前一交易日收盘、收益只能使用 entry/exit 开盘、不能读取 entry 日收盘、缺开盘价硬失败、重复主键硬失败。
- [ ] 运行：`uv run python -m unittest tests.test_trading_schedule tests.test_portfolio_data_source -v`，确认失败。
- [ ] 实现实际交易日顺序、周二调仓窗口、开盘收益和数据来源校验；所有数据 URI 继续通过既有 runtime contract 校验。
- [ ] 运行同一命令，确认通过。
- [ ] 单独提交：`feat: align tuesday open-to-open schedule`。

### 任务 3：实现十组等权、多空和成本

**文件：**

- 新建：`src/factor_miner/portfolio_evaluation.py`
- 新建：`tests/test_portfolio_evaluation.py`

**接口：**

```python
class PortfolioBacktestResult(BaseModel):
    pass

def run_portfolio_backtest(
    panel: pl.LazyFrame,
    benchmark: pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
    policy: PortfolioEvaluationPolicy,
) -> PortfolioBacktestResult:
    pass
```

- [ ] 写失败测试：确定性十组排序、组内等权权重和权重和校验、Q10−Q1/Q9−Q2、14bp成本、换手、空组、无未来开盘价、方向规范化和多空总敞口。
- [ ] 运行：`uv run python -m unittest tests.test_portfolio_evaluation -v`，确认失败。
- [ ] 实现每日组收益、两种多空收益、毛/净收益、换手和成本；禁止逐行 Python 循环和 `fillna(0)`。
- [ ] 运行同一命令，确认通过。
- [ ] 单独提交：`feat: calculate weekly grouped portfolio returns`。

### 任务 4：实现真实交易日日历年化和组合指标

**文件：**

- 新建：`src/factor_miner/portfolio_statistics.py`
- 修改：`src/factor_miner/portfolio_evaluation.py`
- 新建：`tests/test_portfolio_statistics.py`

**接口：**

```python
class PortfolioMetrics(BaseModel):
    pass

def calculate_portfolio_metrics(
    daily_returns: pl.DataFrame,
    benchmark_returns: pl.DataFrame,
    calendar: pl.DataFrame,
) -> PortfolioMetrics:
    pass
```

- [ ] 写失败测试：跨年实际交易日日历年化、净收益 Sharpe、基于财富曲线的最大回撤、超额收益、信息比率、缺失交易日和空收益序列。
- [ ] 运行：`uv run python -m unittest tests.test_portfolio_statistics -v`，确认失败。
- [ ] 实现回测期收益、年化收益、净 Sharpe、最大回撤、年化波动、超额收益、换手和信息比率；所有指标记录年化日历身份和收益口径。
- [ ] 运行同一命令，确认通过。
- [ ] 单独提交：`feat: add calendar-aware portfolio statistics`。

### 任务 5：扩展 IC、RankIC 和衰减诊断

**文件：**

- 新建：`src/factor_miner/ic_diagnostics.py`
- 修改：`src/factor_miner/evaluation.py`
- 新建：`tests/test_ic_diagnostics.py`

**接口：**

```python
class ICDiagnostics(BaseModel):
    pass

def evaluate_ic_horizons(
    factor_panel: pl.LazyFrame,
    horizons: tuple[int, ...],
    policy: EvaluationPolicySpec,
) -> ICDiagnostics:
    pass
```

- [ ] 写失败测试：只接受冻结的 `(1, 3, 5, 10, 20)` 期限、逐日有效样本、`P(IC < -0.02)`、`P(IC > 0.02)`、HAC t 值和结果后不能修改期限。
- [ ] 运行：`uv run python -m unittest tests.test_ic_diagnostics -v`，确认失败。
- [ ] 实现 IC/RankIC 序列、分布、分位数、自相关、年度摘要和衰减长表；主准入仍使用冻结 5 日协议，其他期限只作描述性诊断。
- [ ] 运行同一命令，确认通过。
- [ ] 单独提交：`feat: add frozen multi-horizon ic diagnostics`。

### 任务 6：实现 Barra 暴露和已实现收益归因

**文件：**

- 新建：`src/factor_miner/barra_data_source.py`
- 新建：`src/factor_miner/barra_attribution.py`
- 新建：`tests/test_barra_attribution.py`

**接口：**

```python
class BarraAttributionResult(BaseModel):
    pass

def calculate_barra_attribution(
    portfolio_weights: pl.LazyFrame,
    benchmark_weights: pl.LazyFrame,
    exposures: pl.LazyFrame,
    factor_returns: pl.LazyFrame,
    policy: BarraEvaluationPolicy,
) -> BarraAttributionResult:
    pass
```

- [ ] 写失败测试：暴露与因子收益版本不一致、暴露落后于信号日期、行业或 Size 字段缺失、权重主键重复、已实现贡献对账不成立、缺协方差时错误标记为完整风险分解。
- [ ] 运行：`uv run python -m unittest tests.test_barra_attribution -v`，确认失败。
- [ ] 实现组合暴露、主动暴露、行业/风格贡献、特异残差和数据完整性状态。只有协方差与特异风险合同完整时才计算完整风险分解。
- [ ] 运行同一命令，确认通过。
- [ ] 单独提交：`feat: add barra exposure and attribution`。

### 任务 7：扩展运行产物和终态顺序

**文件：**

- 修改：`src/factor_miner/workflow.py`
- 修改：`src/factor_miner/ledger.py`
- 新建：`tests/test_portfolio_workflow.py`

**接口：**

```python
def run_visible_portfolio_campaign(
    campaign: TrustedVisibleCampaignSpec,
    portfolio_policy: PortfolioEvaluationPolicy,
    barra_policy: BarraEvaluationPolicy,
    sources: PortfolioSources,
    artifact_root: Path,
) -> RunResult:
    pass
```

- [ ] 写失败测试：未 generation seal 不得评价；组合、Barra、清单、指标任一产物缺失不得写通过事件；写入中断时只追加失败/中断事件且不删除字节；所有产物哈希必须绑定 manifest。
- [ ] 运行：`uv run python -m unittest tests.test_portfolio_workflow -v`，确认失败。
- [ ] 按“先写完整暂存产物、校验哈希、原子发布、最后追加终态”的顺序接入组合和 Barra 结果。
- [ ] 运行：`uv run python -m unittest tests.test_portfolio_workflow tests.test_workflow tests.test_ledger -v`，确认通过。
- [ ] 单独提交：`feat: publish portfolio and barra run artifacts`。

### 任务 8：实现批准假设后的自动编排器

**文件：**

- 新建：`src/factor_miner/llm_orchestrator.py`
- 修改：`src/factor_miner/llm_state.py`
- 修改：`src/factor_miner/llm_ledger.py`
- 修改：`src/factor_miner/cli.py`
- 新建：`tests/test_llm_orchestrator.py`
- 新建：`tests/test_llm_run_approved_cli.py`

**接口：**

```python
def run_approved_batch(
    family_id: str,
    approved_hypothesis_ids: tuple[str, ...],
    dependencies: ApprovedBatchDependencies,
) -> ApprovedBatchResult:
    pass

class ExportAuthorizationBundle(BaseModel):
    pass

def prepare_approved_batch_export(
    family_id: str,
    approved_hypothesis_ids: tuple[str, ...],
    dependencies: ApprovedBatchDependencies,
) -> ExportAuthorizationBundle:
    pass

def resume_approved_batch(
    family_id: str,
    dependencies: ApprovedBatchDependencies,
) -> ApprovedBatchResult:
    pass
```

- [ ] 写失败测试：未人工批准不能生成表达式；没有有效 `ExportAuthorizationBundle` 不能调用外部模型；一个槽位成功后恢复不能重复调用；LLM 不能收到 outcome；每条路线最多处理 10 个批准假设；每个批准假设最多 3 个候选；候选失败继续记录并处理其他槽位；没有完整 120 槽 generation seal 不得打开评价器。
- [ ] 运行：`uv run python -m unittest tests.test_llm_orchestrator tests.test_llm_run_approved_cli -v`，确认失败。
- [ ] 实现 `factor-miner llm run-approved` 和 `factor-miner llm resume-approved`；它们自动串联表达式代理、格式校验、semantic lint、候选转换、登记、seal、可见评价、组合回测、Barra 归因和产物发布。
- [ ] 运行：`uv run python -m unittest tests.test_llm_orchestrator tests.test_llm_run_approved_cli tests.test_llm_pilot_cli -v`，确认通过。
- [ ] 单独提交：`feat: orchestrate approved factor research batches`。

### 任务 9：建立 PostgreSQL Dashboard 读模型

**文件：**

- 新建：`dashboard/migrations/001_initial.sql`
- 新建：`src/factor_miner/dashboard_projection.py`
- 新建：`src/factor_miner/dashboard_store.py`
- 修改：`pyproject.toml`
- 修改：`uv.lock`
- 新建：`tests/test_dashboard_projection.py`

**接口：**

```python
def project_run_artifacts(
    artifact_root: Path,
    run_id: str,
    store: DashboardStore,
) -> ProjectionResult:
    pass

def verify_projection(
    artifact_root: Path,
    run_id: str,
    store: DashboardStore,
) -> ProjectionResult:
    pass
```

数据库至少包含：

```text
research_runs
candidates
candidate_metrics
portfolio_metrics
portfolio_daily
barra_exposure_summary
barra_attribution
artifact_refs
```

- [ ] 写失败测试：同一 run 重复投影幂等；产物哈希变化拒绝更新；缺失 manifest 拒绝投影；只读查询账号不能修改；删除并重建读模型后摘要哈希一致。
- [ ] 运行：`uv run python -m unittest tests.test_dashboard_projection -v`，确认失败。
- [ ] 采用 `psycopg` 参数化 SQL 建表和投影；数据库 URL 只从服务器私有环境读取，不出现在 CLI、提示、日志或产物中。
- [ ] 运行同一命令，确认通过。
- [ ] 单独提交：`feat: add postgres dashboard read model`。

### 任务 10：实现只读可视化 Dashboard

**文件：**

- 新建：`dashboard/app.py`
- 新建：`dashboard/pages/1_批次总览.py`
- 新建：`dashboard/pages/2_IC诊断.py`
- 新建：`dashboard/pages/3_分组回测.py`
- 新建：`dashboard/pages/4_Barra归因.py`
- 新建：`dashboard/pages/5_运行审计.py`
- 新建：`tests/test_dashboard_contract.py`

**接口：**

```python
def load_dashboard_snapshot(
    run_id: str,
    store: DashboardStore,
) -> DashboardSnapshot:
    pass
```

- [ ] 写失败测试：Dashboard 只能读取已发布 run；不能读取暂存目录；不能修改候选或政策；不存在风险模型时显示 `realized_attribution_only`；指标标签区分毛/净收益和主/描述性统计。
- [ ] 运行：`uv run python -m unittest tests.test_dashboard_contract -v`，确认失败。
- [ ] 实现 Streamlit 页面、候选筛选、10 组收益表、Q10−Q1/Q9−Q2 曲线、IC 图、Barra 暴露图和审计页；默认绑定 `127.0.0.1`，不提供写操作。
- [ ] 运行同一命令，确认通过。
- [ ] 单独提交：`feat: add read-only factor research dashboard`。

### 任务 11：完成公司 Linux 端到端验收

**文件：**

- 修改：`docs/contracts/v1自动化研究与可视化运行合同.md`
- 修改：`README.md`
- 修改：`docs/START_HERE.md`
- 新建：`tests/test_v1_synthetic_e2e.py`

- [ ] 写合成端到端测试：四条路线各 10 个假设槽、每个假设 3 个候选、批准后自动生成、候选失败终态、周二休市顺延、open-to-open、十组收益、两种多空、成本、IC 诊断、Barra 对账、产物发布、PostgreSQL 投影和 Dashboard 读取。
- [ ] 运行：`uv run python -m unittest tests.test_v1_synthetic_e2e -v`，确认失败。
- [ ] 实现合成夹具和运行合同；Mac 测试不得读取真实服务器文件、真实个股或真实结果。
- [ ] 运行：`uv run python -m unittest discover -s tests -v`，确认本地全量通过。
- [ ] 在公司 Linux 使用冻结提交执行：

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
factor-miner doctor --env-file /data/factor_miner_artifacts/config/company_a_share.env
APPROVED_FAMILY_ID="${FM_APPROVED_FAMILY_ID:?服务器私有环境必须提供已批准的 family ID}"
factor-miner llm authorize-scope \
  "$APPROVED_FAMILY_ID" \
  /data/factor_miner_artifacts/private_config/corporate_external_policy.local.json \
  --output /data/factor_miner_artifacts/private_config/v05_expression_scope_authorization.json
factor-miner llm run-approved \
  "$APPROVED_FAMILY_ID" \
  /data/factor_miner_artifacts/private_config/v05_hypothesis_approved.json \
  /data/factor_miner_artifacts/private_config/corporate_external_policy.local.json \
  /data/factor_miner_artifacts/private_config/v05_expression_public_payload.json \
  /data/factor_miner_artifacts/private_config/v05_field_registry.local.json \
  /data/factor_miner_artifacts/private_config/v05_expression_scope_authorization.json \
  --artifact-root /data/factor_miner_artifacts
COMPLETED_RUN_ID="${FM_COMPLETED_RUN_ID:?服务器私有环境必须提供已完成的 run ID}"
python -m dashboard.project "$COMPLETED_RUN_ID" \
  --artifact-root /data/factor_miner_artifacts
```

- [ ] 验收交易日、数据 release、Barra 模型身份、组合收益和所有产物哈希；任何数据缺口或时点不一致均以失败终态结束。
- [ ] 单独提交：`docs: add v1 automated research and dashboard contract`。

## V1 验收标准

V1 只有同时满足以下条件才算完成：

1. 用户批准假设后，不需要手工复制公式或逐个提交网页回测。
2. 每条生成路线一次最多处理 10 个批准假设，每个批准假设最多生成 3 个因子设计，四条路线共 120 个候选槽位，所有槽位都有不可变终态。
3. 候选生成完成后，LLM 看不到任何 IC、收益、回撤、Sharpe 或 Barra 结果。
4. 周二开盘、open-to-open、实际交易日历、十组等权、多空定义、沪深300和 14bp 成本都有政策哈希。
5. Sharpe、最大回撤、年化收益和信息比率均基于净收益口径并有定义。
6. 行业、Size 和风格暴露能够按调仓日对账；Barra 数据不完整时不会伪造完整风险分解。
7. 结果先完整发布再写终态事件；中断可恢复；重复运行不产生重复候选或重复数据库事实。
8. Dashboard 只能读取已发布产物和读模型，不能改变研究状态。
9. PostgreSQL 删除后可以从服务器不可变 JSON/JSONL/Parquet 产物重建相同读模型。
10. Mac 只通过合成测试；真实数据、真实结果、密钥和服务器私有配置不进入 Git。

## 执行顺序

先完成 V0.5 服务器真实单候选试运行并记录明确终态，再按任务 1 至任务 11 顺序实施 V1。任务 1 至任务 6 先建立可信金融计算层，任务 7 至任务 8 串联自动化，任务 9 至任务 10 提供可视化，任务 11 才进行真实服务器验收。不要先做 Dashboard 页面，也不要用 PostgreSQL 掩盖组合评价或 Barra 数据合同缺失。
