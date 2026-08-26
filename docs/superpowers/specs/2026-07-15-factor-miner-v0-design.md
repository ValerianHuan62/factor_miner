# Factor Miner V0 Design

- Status: Accepted for implementation planning
- Date: 2026-07-15
- Project: standalone `factor_miner`

## 1. Goal

构建一个完全独立于 `huan_quant` 的因子候选研究引擎。V0 接收人工编写或录制的结构化 CandidateFactorSpec，在公司服务器上完成 typed DSL 校验、确定性 Polars 编译、真实数据 raw factor 计算、visible RankIC/HAC/Bonferroni 与冗余 Gate，并留下可重放的 JSON/JSONL 审计记录。

V0 的终点是 `visible_passed candidate`，不是认证因子。V0 不查看 sealed OOS，也不自动把因子导入任何生产仓库。

## 2. Alternatives Considered

### 2.1 在 `huan_quant` 内实现

优点是能直接复用当前数据和研究代码；缺点是把引擎绑定到单一仓库、Skill、目录和 Registry，不满足独立使用目标。拒绝。

### 2.2 独立核心，但运行时 import `huan_quant` adapter

初期代码较少，但独立仓库仍不能单独运行，两个仓库必须同步版本。拒绝。

### 2.3 独立引擎 + 独立数据合同

核心只依赖 `DataSource`、`Ledger` 等协议；第一个 adapter 按显式 manifest 读取公司 A 股服务器数据，不 import `huan_quant`。未来数据源只增加 adapter。采用。

## 3. Scope

### Included

- immutable Hypothesis/Candidate/Campaign schemas
- 有限白名单 typed AST 与 canonical hash
- AST → deterministic Polars expression plan
- fail-closed server runtime profile
- `company_a_share` DataSource adapter
- raw factor compute 与质量指标
- visible daily cross-sectional Spearman RankIC
- 冻结参数的 HAC 双侧显著性检验
- 以预登记 `max_hypotheses` 为 family size 的 Bonferroni Gate
- AST 重复与输出相关性冗余 Gate
- immutable artifacts 与 hash-chained append-only JSONL ledger
- CLI、合成测试、服务器真实数据 Smoke 和一个 bounded visible campaign

### Not Included

- live LLM、prompt、retrieve/generate/evaluate/distill
- Experience Memory、trajectory evolution、自动修复或 Bayesian search
- PostgreSQL、Web UI、任务队列、并行或分布式 worker
- sealed OOS、Evidence 晋级、正式认证或生产发布
- 策略回测、组合构建、成本模型或下游训练
- `huan_quant` import、自动 exporter 或 Factor Zoo 写入
- 任意 Python/program-level factor

## 4. Architecture

依赖方向固定为：

```text
CLI / Workflow
  → Schemas / DSL / Compiler / Evaluation / Redundancy
  → Ports: DataSource, Ledger
  → Adapters: CompanyAShareDataSource, JsonlLedger
```

任何 domain、DSL、compiler、statistics 模块均不得 import Skill、`huan_quant`、服务器路径或具体数据文件。

计划中的源码结构：

```text
src/factor_miner/
├── canonical.py       # canonical JSON 与 SHA-256
├── errors.py          # 稳定错误码
├── runtime.py         # server/synthetic profile 与 fail-closed Gate
├── schema.py          # Hypothesis/Candidate/Campaign/Result
├── dsl.py             # AST、白名单、类型/时序/复杂度检查
├── compiler.py        # AST → Polars expression plan
├── ledger.py          # immutable docs + hash-chained JSONL
├── data_source.py     # DataSource protocol 与 provenance
├── company_a_share.py # 第一个真实数据 adapter
├── compute.py         # raw factor 执行与质量报告
├── evaluation.py      # daily RankIC 与 coverage
├── statistics.py      # HAC 与 Bonferroni
├── redundancy.py      # structural/output redundancy
├── workflow.py        # 单 cohort 状态机
└── cli.py             # doctor/validate/compile/run-smoke/run-visible/ledger-verify
```

## 5. Core Objects

### HypothesisSpec

必须包含：事前 claim、mechanism、expected sign、observable proxy、独立验证数据与方法、competing explanations、baseline/reference、failure modes、falsification path、source references 和 `mechanism_status`。V0 准入只允许 `mechanism_unverified`；统计结果不能自动改成 `supported`。

### CandidateFactorSpec

包含 schema version、HypothesisSpec、typed AST、required fields、max lookback、availability、provenance 和 created-at。candidate ID 由 canonical spec SHA-256 派生，不接受人工指定。修改任一字段产生新 ID。

### CampaignSpec

在 outcome 前冻结：有顺序的 candidate IDs、`max_hypotheses`、visible 区间、label、rank mask、HAC max lags、`alpha`、最低有效日期、每日最低股票数、最低 median coverage、结构/输出冗余阈值和 reference pool。candidate 顺序就是事前优先级，不能按结果重排；`max_hypotheses` 必须不小于登记候选数，每个变体占一个 slot。

### TrialEvent

每个事件包含 event/sequence/candidate/campaign/run IDs、UTC 时间、event type、status、outcome-exposed、failure code、spec/code/data/config hashes、artifact refs、previous event hash 和 event hash。纠错通过追加 `supersedes_event_id`，不更新旧行。

## 6. Data Flow

1. CLI 加载 CandidateFactorSpec，schema 校验后生成 canonical JSON 与 candidate ID。
2. JsonlLedger 原子写入 candidate document，并追加 `candidate_registered`。
3. CampaignSpec 冻结候选集合和完整评价协议；在读取任何 label/outcome 前登记。
4. DSL validator 拒绝非法字段、未知算子、未来引用、超预算 AST 和标签泄漏。
5. compiler 生成 immutable execution plan，保存 AST hash、plan hash、required fields 和 lookback。
6. 真实模式先运行 doctor：验证 Linux、`/data` allowlist、release/manifest、复权、calendar、状态表 cutoff、只读 QuantLake 与可写 artifact root。
7. compute 仅生成 raw factor；保留 NaN，并报告 warmup、coverage、inf、全空截面和 factor hash。
8. evaluator 使用冻结的 `valid_for_factor_rank` 与 `label_o2o_5d`，逐日计算截面 Spearman RankIC。
9. statistics 对 RankIC 序列执行 campaign 冻结 max lags 的 HAC，保存双侧 raw p；Bonferroni 为 `min(1, p_raw * max_hypotheses)`。
10. redundancy 先检查 canonical AST/signature，再检查 visible 区间总体及分年度输出 Spearman；候选只与 reference pool 和事前排在它前面的候选比较，禁止按结果挑代表。
11. expected sign、数据质量、Bonferroni adjusted p 和冗余 Gate 全部通过才写 `visible_passed`；其余写明确失败终态。
12. workflow 原子写 run artifacts 后追加 terminal event。任何 outcome-exposed run 均永久计入 campaign 历史。

## 7. File Storage

以下目录全部位于 `FM_ARTIFACT_ROOT` 下；RuntimeProfile 由该根目录确定性派生 state 与 runs 路径，不再增加第二套可变根目录。

```text
state/
├── candidates/<candidate_id>.json
├── campaigns/<campaign_id>.json
└── ledger/trials.jsonl

artifacts/runs/<run_id>/
├── manifest.json
├── execution_plan.json
├── quality.json
├── metrics.json
├── redundancy.json
└── candidate_package.json
```

JSON documents 采用临时文件、`fsync` 和同文件系统 `os.replace`。JSONL 在 Linux/macOS 使用进程锁、单行 append、flush 与 `fsync`；V0 禁止第二个 writer。每行 hash 包含 previous hash，启动时完整校验。损坏、截断或 hash 不符时停止，不自动修复。

## 8. Statistical and Research Semantics

- 每日截面 Spearman RankIC 是 V0 主统计量。
- HAC 使用双侧检验；expected sign 是独立 Gate，禁止结果后改变方向。
- visible pass 使用 `p_adjusted <= alpha`，其中 family size 是预登记 `max_hypotheses`。
- 编译失败和未暴露 outcome 的候选仍占预登记 budget；读取过 label/IC 的 trial 必须 `outcome_exposed=true`。
- 结构或输出高冗余候选不得通过，但记录不删除。
- 机制状态独立于统计状态；`visible_passed + mechanism_unverified` 是合法结果。
- V0 没有 sealed test，因此任何输出最多是研究候选。

## 9. Error Handling

稳定错误码至少包括：

```text
RUNTIME_BOUNDARY_ERROR
SPEC_SCHEMA_INVALID
DSL_TYPE_ERROR
LOOKAHEAD_DETECTED
LABEL_LEAKAGE_DETECTED
DATA_RELEASE_MISMATCH
STATE_COVERAGE_INCOMPLETE
FIELD_MISSING
ADJUSTMENT_CONVENTION_UNKNOWN
FACTOR_ALL_NULL
FACTOR_COVERAGE_TOO_LOW
STAT_FAMILY_NOT_FROZEN
INSUFFICIENT_VALID_DATES
REDUNDANCY_THRESHOLD_EXCEEDED
LEDGER_CONCURRENT_WRITER
LEDGER_CORRUPT
```

主流程失败时 CLI 返回非零状态，追加失败事件，但不伪造 metrics，不改变数据、label、窗口、阈值或 profile 重试同一 trial。

## 10. Testing

纯合成测试覆盖 canonical hash、schema、AST、未来引用、operator golden cases、compiler determinism、ledger 原子性/损坏检测、RankIC/HAC/Bonferroni、重复因子和 shuffled-label null。合成数据必须程序生成，不能抽样或脱敏自任何真实行情。

公司服务器测试依次为：data contract doctor、bounded real-data Smoke、完整 visible campaign。Smoke 只验证接线，不形成研究结论。所有真实 run 记录 commit、lock hash、config/spec/campaign hash 和完整数据 provenance，并验证 `/data/quantlake` 未被写入。

## 11. Dependencies

按 2026-07-15 官方 GitHub 仓库状态选择：

| Project | Purpose | Stars | Latest release observed | Status |
| --- | --- | ---: | --- | --- |
| [uv](https://github.com/astral-sh/uv) | 环境与 lockfile | 86.4k | 0.11.16 | 持续更新 |
| [Polars](https://github.com/pola-rs/polars) | Lazy/向量化因子计算 | 38.6k | 1.41.0, 2026-05-22 | 持续更新 |
| [Pydantic](https://github.com/pydantic/pydantic) | immutable schema/validation | 27.8k | 2.13.4, 2026-05-06 | 持续更新 |
| [Typer](https://github.com/fastapi/typer) | CLI | 19.8k | 0.26.8, 2026-06-26 | 持续更新 |
| [statsmodels](https://github.com/statsmodels/statsmodels) | HAC inference | 11.5k | 0.14.6, 2025-12-05 | 维护中 |

V0 不引入 workflow engine、数据库 ORM、LLM SDK 或自定义分布式计算。JSON/canonical hash、Linux file lock 和 atomic rename 使用 Python 标准库。

## 12. Acceptance Criteria

- 新环境只需要本仓库、锁定依赖、显式 server profile 和满足合同的数据源，不需要 `huan_quant`、Skill 或数据库。
- 一个合法手工 spec 能完成 validate、compile、server compute、visible RankIC/HAC/Bonferroni、冗余和候选包。
- future factor、label leakage、duplicate factor、null factor 和 shuffled-label factor 均以预期原因失败。
- 中断和失败可由 ledger 完整追溯；candidate/campaign 不可原地改写。
- 相同 spec/release/commit/config 产生相同 plan 与 raw factor hash。
- 所有输出明确标记 `visible_only=true`、`sealed_oos_used=false`、`production_eligible=false`。

## 13. Legacy Constraint Mapping

原 `huan_quant` 的研究认知、反证、未来函数、原始/预处理分层、服务器数据边界、多重检验、失败留痕和冗余规则已迁入本仓库约束。`pipeline/llm_factor` 路径、Skill 路由、PostgreSQL-only、个人账号、DolphinDB 和 `huan_quant` L0-L8 接线不迁移。

原 `2026-07-14-llm-factor-discovery-server-v0.md` 是历史设计输入，不再是本项目执行计划；新的唯一实施计划是 `docs/superpowers/plans/2026-07-15-factor-miner-v0.md`。
