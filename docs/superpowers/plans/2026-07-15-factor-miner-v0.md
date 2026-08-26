# Factor Miner V0 可见验证候选因子实施计划

> **给代理执行者：**必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项实施本计划。使用复选框（`[ ]`）跟踪步骤完成情况。

**目标：**构建一个独立、服务器优先的流水线，将预先登记的人工 `CandidateFactorSpec` 文件转换为可审计、通过可见验证的候选因子。

**架构：**领域层负责不可变 schema、typed DSL、确定性编译、评价、统计和冗余检查。数据与持久化通过端口抽象，配套公司 A 股 Parquet 适配器和单写入者 JSON/JSONL 账本；任何模块都不得导入 `huan_quant`、Skill、数据库或个人路径。

**技术栈：**Python 3.12、uv、Pydantic 2、Polars 1、statsmodels 0.14、Typer、Python unittest、JSON/JSONL、Parquet。

## 全局约束

- 真实行情计算、IC、显著性和冗余检查只能在公司 Linux 服务器上执行。
- 禁止使用 Mac 行情；合成测试的每个数值都必须由代码生成。
- `/data/quantlake` 只读，artifact 必须写入单独且显式配置的 `/data` 路径。
- V0 不包含 live LLM、PostgreSQL、sealed OOS、回测、Skill、`huan_quant` 导入或自动晋级生产。
- Candidate 和 Campaign spec 不可变，必须在读取任何结果前完成登记。
- 候选公式只能使用白名单 typed AST；禁止 `eval`、`exec`、动态导入、任意 Python、未来引用或标签字段。
- 原始因子计算不得标准化、排名、winsorize、中性化或用零填充空值。
- 可见推断使用逐日截面 Spearman RankIC、冻结的 HAC 最大滞后阶数和 Bonferroni family size。
- 所有生成、失败、中断和冗余 trial 都必须保留在追加式账本中。
- 通过结果只能标记为 visible-only，且 `sealed_oos_used=false`、`production_eligible=false`。
- 运行时 fallback 不得改变数据源、标签、切分、mask、窗口、统计方法或存储后端。

---

### 任务 1：Python 包基础与规范化哈希

**文件：**

- 创建：`.python-version`
- 创建：`pyproject.toml`
- 创建：`src/factor_miner/__init__.py`
- 创建：`src/factor_miner/errors.py`
- 创建：`src/factor_miner/canonical.py`
- 创建：`tests/__init__.py`
- 创建：`tests/test_canonical.py`
- 创建：`tests/test_import_boundary.py`

**接口：**

- 产出：`FactorMinerError`、`FailureCode`、`canonical_json_bytes(value)`、`sha256_json(value)`。

- [x] **步骤 1：添加锁定的包定义**

~~~toml
[project]
name = "factor-miner"
version = "0.1.0"
description = "可审计的因子候选发现流水线"
requires-python = ">=3.12,<3.13"
dependencies = [
  "polars>=1.41.0,<2",
  "pydantic>=2.13.4,<3",
  "statsmodels>=0.14.6,<0.15",
  "typer>=0.26.8,<0.27",
]

[project.scripts]
factor-miner = "factor_miner.cli:app"

[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
~~~

将 `.python-version` 设置为精确的 `3.12`。运行 `uv lock` 和 `uv sync`，并提交 `uv.lock`。

- [x] **步骤 2：编写规范化 JSON 和导入边界失败测试**

~~~python
import math
import unittest

from factor_miner.canonical import canonical_json_bytes, sha256_json


class CanonicalJsonTest(unittest.TestCase):
    def test_key_order_does_not_change_hash(self):
        self.assertEqual(sha256_json({"b": 2, "a": 1}), sha256_json({"a": 1, "b": 2}))

    def test_non_finite_number_is_rejected(self):
        with self.assertRaises(ValueError):
            canonical_json_bytes({"x": math.nan})
~~~

导入边界测试必须扫描 `src/factor_miner`，拒绝出现文本 `huan_quant`、`/Users/`、`DolphinDB`、`eval(` 和 `exec(`。

- [x] **步骤 3：运行测试并确认预期失败**

运行： uv run python -m unittest tests.test_canonical tests.test_import_boundary -v

预期：由于 `factor_miner.canonical` 尚不存在，测试失败。

- [x] **步骤 4：实现确定性规范化 JSON 和稳定错误**

`canonical_json_bytes` 必须使用 `json.dumps`，参数固定为 `sort_keys=True`、`separators=(",", ":")`、`ensure_ascii=False` 和 `allow_nan=False`，然后编码为 UTF-8。`sha256_json` 返回完整的小写 SHA-256 十六进制摘要。`FailureCode` 使用 `StrEnum`，包含设计文档错误章节列出的全部代码；`FactorMinerError` 携带 code 和 message，不得吞掉原始异常原因。

- [x] **步骤 5：验证并提交**

运行： uv run python -m unittest tests.test_canonical tests.test_import_boundary -v

预期：通过。

运行： git add .python-version pyproject.toml uv.lock src/factor_miner/__init__.py src/factor_miner/errors.py src/factor_miner/canonical.py tests/__init__.py tests/test_canonical.py tests/test_import_boundary.py

提交：`git commit -m "build: initialize standalone factor miner package"`。

### 任务 2：故障关闭的运行时配置

**文件：**

- 创建：`src/factor_miner/runtime.py`
- 创建：`configs/company_a_share.env.example`
- 创建：`tests/test_runtime.py`

**接口：**

- 产出：`ExecutionMode`、`RuntimeProfile`、`load_runtime_profile(env, platform_name)`、`assert_real_data_allowed(profile)`。
- 使用：`FailureCode` 和 `FactorMinerError`。

- [x] **步骤 1：编写运行时拒绝测试**

以下情况必须分别测试：

~~~python
class RuntimeBoundaryTest(unittest.TestCase):
    def test_visible_mode_rejects_darwin(self):
        env = complete_env(mode="visible")
        with self.assertRaisesRegex(FactorMinerError, "RUNTIME_BOUNDARY_ERROR"):
            load_runtime_profile(env, platform_name="Darwin")

    def test_quantlake_must_be_read_only_root(self):
        env = complete_env(mode="visible")
        env["FM_QUANTLAKE_ROOT"] = "/Users/huan/data"
        with self.assertRaises(FactorMinerError):
            load_runtime_profile(env, platform_name="Linux")

    def test_artifacts_cannot_live_inside_quantlake(self):
        env = complete_env(mode="visible")
        env["FM_ARTIFACT_ROOT"] = "/data/quantlake/results"
        with self.assertRaises(FactorMinerError):
            load_runtime_profile(env, platform_name="Linux")
~~~

还必须拒绝缺失 manifest、复权约定、行情/状态/标签 URI、schema/calendar/state version，以及仓库内真实数据路径的情况。

- [x] **步骤 2：运行聚焦测试**

运行： uv run python -m unittest tests.test_runtime -v

预期：由于 `runtime.py` 不存在，测试失败。

- [x] **步骤 3：实现运行时合同**

`ExecutionMode` 的取值为 `design`、`synthetic`、`smoke` 和 `visible`。`RuntimeProfile` 不可变，包含全部 `FM_` 环境变量值及解析后的 `Path` 对象。`design` 和 `synthetic` 可以使用临时路径，且绝不打开真实数据。`smoke` 和 `visible` 要求 Linux、`quantlake_root` 精确等于 `/data/quantlake`、所有数据 URI 位于 `/data` 下、artifact root 位于 `/data` 下且不在 QuantLake 内，并且所有 provenance 字段非空。

环境变量示例文件必须列出每个必需变量并附中文注释。依赖具体路径的值必须有意留空；加载器将空值视为缺失，不得擅自生成默认值。

- [x] **步骤 4：运行全部基础测试并提交**

运行： uv run python -m unittest tests.test_canonical tests.test_import_boundary tests.test_runtime -v

预期：通过。

运行： git add src/factor_miner/runtime.py configs/company_a_share.env.example tests/test_runtime.py

提交：`git commit -m "feat: enforce server runtime boundary"`。

### 任务 3：不可变的假设、候选和 Campaign schema

**文件：**

- 创建：`src/factor_miner/schema.py`
- 创建：`tests/helpers.py`
- 创建：`tests/test_schema.py`

**接口：**

- 产出：`ExpectedSign`、`MechanismStatus`、`HypothesisSpec`、`FactorNode`、`CandidateFactorSpec`、`RegisteredCandidate`、`CampaignSpec`、`registered_candidate(spec)`、`campaign_id(spec)`。
- 使用：`sha256_json`。

- [x] **步骤 1：编写 schema 准入和往返测试**

~~~python
class SchemaTest(unittest.TestCase):
    def test_candidate_id_is_content_addressed(self):
        left = registered_candidate(valid_candidate())
        right = registered_candidate(valid_candidate())
        self.assertEqual(left.candidate_id, right.candidate_id)
        self.assertEqual(left.spec_hash, right.spec_hash)

    def test_missing_independent_verification_is_rejected(self):
        payload = valid_candidate().model_dump(mode="json")
        payload["hypothesis"]["independent_verification"] = ""
        with self.assertRaises(ValidationError):
            CandidateFactorSpec.model_validate(payload)

    def test_campaign_budget_covers_registered_candidates(self):
        payload = valid_campaign().model_dump(mode="json")
        payload["max_hypotheses"] = 0
        with self.assertRaises(ValidationError):
            CampaignSpec.model_validate(payload)
~~~

还必须测试：禁止额外字段、无时区时间戳、拒绝从输入直接提供 candidate ID、无效 alpha、重复 candidate ID，以及结果产生后用文本覆盖假设。

- [x] **步骤 2：确认失败**

运行： uv run python -m unittest tests.test_schema -v

预期：由于 `schema.py` 不存在，测试失败。

- [x] **步骤 3：实现冻结的 Pydantic 模型**

所有模型使用 `ConfigDict(frozen=True, extra="forbid")`。`HypothesisSpec` 必须包含 claim、mechanism、expected_sign、observable_proxy、independent_verification、competing_explanations、baseline_reference、failure_modes、falsification_path、source_refs，并且 `mechanism_status=mechanism_unverified`。`CandidateFactorSpec` 包含 `spec_version="1"`、hypothesis、expression、required_fields、max_lookback、availability、created_at 和 provenance。

`RegisteredCandidate` 包含由 `"cand_"` 加前 24 位哈希字符组成的 candidate_id、完整 spec_hash 和不可变 spec。`CampaignSpec` 包含可见区间日期、有序 candidate ID 元组、max_hypotheses、alpha、label_column、rank_mask_column、hac_max_lags、min_valid_dates、min_names_per_date、min_median_coverage、max_abs_output_correlation 和 reference_factor_columns。候选顺序是冗余比较时冻结的优先级。其内容哈希生成 campaign_id。

- [x] **步骤 4：验证序列化并提交**

运行： uv run python -m unittest tests.test_schema -v

预期：测试通过，规范化模型 JSON 往返序列化后哈希不漂移。

运行： git add src/factor_miner/schema.py tests/helpers.py tests/test_schema.py

提交：`git commit -m "feat: add immutable candidate and campaign schemas"`。

### 任务 4：Typed DSL 校验与规范化 AST

**文件：**

- 创建：`src/factor_miner/dsl.py`
- 创建：`tests/test_dsl.py`

**接口：**

- 产出：`DslLimits`、`AstMetadata`、`validate_ast(node, allowed_fields, forbidden_fields, limits)`、`canonical_ast(node)`、`canonical_ast_hash(node)`。
- 使用：`FactorNode` 和 `FailureCode`。

- [x] **步骤 1：编写正例、反例和等价性测试**

测试必须覆盖：

- 合法的 20 日收盘价动量
- 拒绝负 delay、居中 rolling、标签字段和未知算子
- 拒绝不在 5/10/20/40/60/120 集合内的窗口
- 拒绝超过 15 个节点、深度超过 5、lookback 超过 130
- `add(a,b)` 与 `add(b,a)` 具有相同规范化哈希
- `sub(a,b)` 与 `sub(b,a)` 具有不同哈希
- 拒绝仅由常数组成的表达式

运行： uv run python -m unittest tests.test_dsl -v

预期：由于 `dsl.py` 不存在，测试失败。

- [x] **步骤 2：实现语法和递归校验器**

叶子操作为 field 和 const。算子为 add、sub、mul、div、neg、abs、delay、delta、rolling_sum、rolling_mean、rolling_std、rolling_min、rolling_max 和 rolling_corr。delay 必须非负，所有 rolling 窗口必须使用固定集合。校验参数个数，推导 required fields 和 lookback；仅对 add 和 mul 的子节点排序以生成规范化表示。编译前拒绝所有禁止字段。

- [x] **步骤 3：运行 DSL 和 schema 测试**

运行： uv run python -m unittest tests.test_schema tests.test_dsl -v

预期：通过。

- [x] **步骤 4：提交**

运行： git add src/factor_miner/dsl.py tests/test_dsl.py

提交：`git commit -m "feat: validate canonical typed factor AST"`。

### 任务 5：确定性的 Polars 编译器

**文件：**

- 创建：`src/factor_miner/compiler.py`
- 创建：`tests/test_compiler.py`

**接口：**

- 产出：`CompiledFactorPlan`、`compile_candidate(candidate, allowed_fields)`、`build_polars_expr(node)`。
- 使用：`validate_ast` 和 `canonical_ast_hash`。

- [x] **步骤 1：编写编译器黄金测试**

创建一个由程序生成、包含两个资产和 30 个日期的 Polars DataFrame，验证 `rolling_mean(close, 5)`、`delay(close, 1)`、`delta(close, 5)` 以及安全除法与手工计算的 Polars 列一致。验证除数为零时结果是 null，而不是通过 epsilon 调整。扫描编译器源码，拒绝 `eval`、`exec` 和动态导入。

运行： uv run python -m unittest tests.test_compiler -v

预期：由于 `compiler.py` 不存在，测试失败。

- [x] **步骤 2：实现白名单映射**

`CompiledFactorPlan` 包含 candidate_id、ast_hash、plan_hash、required_fields、required_lookback、availability 和 expression 元数据。`build_polars_expr` 将每个允许的节点映射为作用于按 asset/date 排序数据的 Polars 表达式，时序操作使用 `over("asset")`。安全除法在除数为零时返回 null 表达式。

编译器不得 collect 数据、执行截面变换或对齐未来标签。

- [x] **步骤 3：验证确定性的计划哈希**

对同一候选执行两次编译，断言模型导出结果和哈希完全一致。运行：

uv run python -m unittest tests.test_dsl tests.test_compiler -v

预期：通过。

- [x] **步骤 4：提交**

运行： git add src/factor_miner/compiler.py tests/test_compiler.py

提交：`git commit -m "feat: compile factor AST to deterministic Polars plans"`。

### 任务 6：不可变文档与哈希链 JSONL 账本

**文件：**

- 创建：`src/factor_miner/ledger.py`
- 创建：`tests/test_ledger.py`

**接口：**

- 产出：`EventType`、`TrialEvent`、`LedgerPaths`、`JsonlLedger.register_candidate()`、`register_campaign()`、`append_event()`、`read_events()`、`verify()`。
- 使用：`canonical_json_bytes` 和 `sha256_json`。

- [x] **步骤 1：编写账本完整性测试**

使用 `TemporaryDirectory` 测试：

- candidate 和 campaign 文档按内容寻址，不能被不同字节覆盖
- 两个追加事件的 sequence 单调递增，previous hash 正确
- 拒绝重复 event ID
- 修改 JSONL 中此前的任意字节后，verify 抛出 `LEDGER_CORRUPT`
- 最后一行被截断时，verify 失败
- 第二个非阻塞 writer 锁抛出 `LEDGER_CONCURRENT_WRITER`
- 纠错必须创建带 `supersedes_event_id` 的新事件

运行： uv run python -m unittest tests.test_ledger -v

预期：由于 `ledger.py` 不存在，测试失败。

- [x] **步骤 2：实现原子文档写入**

将规范化字节写入同目录临时文件，执行 flush、fsync 和 os.replace。如果目标路径已存在，相同字节视为幂等，不同字节则失败。绝不更新或删除已有 candidate/campaign 文档。

- [x] **步骤 3：实现单写入者事件日志**

使用 `fcntl.flock` 配合 `LOCK_EX` 和 `LOCK_NB`。持锁后验证当前最后一个事件，分配 sequence，基于排除 event_hash 的事件计算 event_hash，使用 `O_APPEND` 追加恰好一行规范化 JSON，并执行 flush 和 fsync。verify 重放完整哈希链并检查 event ID 唯一性。

- [x] **步骤 4：验证并提交**

运行： uv run python -m unittest tests.test_ledger -v

预期：通过。

运行： git add src/factor_miner/ledger.py tests/test_ledger.py

提交：`git commit -m "feat: add append-only factor trial ledger"`。

### 任务 7：DataSource 合同与公司 A 股适配器

**文件：**

- 创建：`src/factor_miner/data_source.py`
- 创建：`src/factor_miner/company_a_share.py`
- 创建：`tests/test_data_source.py`

**接口：**

- 产出：`DataProvenance`、`DataRequest`、`DataSource Protocol`、`CompanyAShareDataSource.inspect()`、`scan(request)`、`validate_contract()`。
- 使用：`RuntimeProfile` 以及 `docs/contracts/company-a-share-data.md` 中定义的标准列。

- [x] **步骤 1：编写合成 Parquet 合同测试**

在 `TemporaryDirectory` 中生成行情、状态和标签 Parquet 文件。验证合法 fixture 返回包含标准列的 LazyFrame。分别为 date/asset 重复、mask 列缺失、cutoff 不一致、状态表过期、派生 mask 错误、provenance 缺失，以及真实模式下 QuantLake 根目录可写等情况增加失败测试。

任何 fixture 都不得复制真实行情或 Mac 行情。

运行： uv run python -m unittest tests.test_data_source -v

预期：由于适配器不存在，测试失败。

- [x] **步骤 2：定义端口**

`DataRequest` 包含 start、end、required_fields 和 warmup_days。`DataProvenance` 包含数据合同中的全部字段，以及 code_commit 和 config_hash。`DataSource` 在 scan() 前提供 inspect()；只有 provenance 和合同校验通过后，scan 才允许执行。

- [x] **步骤 3：实现显式 URI 的 Parquet 适配器**

适配器只能从 `RuntimeProfile` 接收 market_uri、state_uri 和 label_uri。使用 `pl.scan_parquet` 做列和日期投影，按 date/asset 连接，验证主键唯一性和精确 mask 语义，并只返回请求字段、标识列、mask 和标签。绝不能通过导入或遍历 `huan_quant` 自动发现路径。

- [x] **步骤 4：验证并提交**

运行： uv run python -m unittest tests.test_runtime tests.test_data_source -v

预期：通过。

运行： git add src/factor_miner/data_source.py src/factor_miner/company_a_share.py tests/test_data_source.py

提交：`git commit -m "feat: add contract-driven A-share data adapter"`。

### 任务 8：原始因子计算与质量报告

**文件：**

- 创建：`src/factor_miner/compute.py`
- 创建：`tests/test_compute.py`

**接口：**

- 产出：`FactorQuality`、`FactorArtifact`、`compute_raw_factor(plan, source, request, output_path)`。
- 使用：`CompiledFactorPlan` 和 `DataSource`。

- [x] **步骤 1：编写原始层测试**

测试合法 rolling 因子、warmup 删除、稳定的 date/asset 排序、空值保留、inf 归一化为 null、全空拒绝和低覆盖率报告。断言源码不包含 zscore、winsorize、neutralize、`fill_null(0)` 或 rank。

运行： uv run python -m unittest tests.test_compute -v

预期：由于 `compute.py` 不存在，测试失败。

- [x] **步骤 2：实现流式计算**

只投影 required columns，包含 required lookback，计算已编译表达式，将非有限输出归一化为 null，仅对最终 raw value 应用 valid_for_factor_compute，从保存区间移除 warmup 行，并在 QuantLake 外原子写入 Parquet。`FactorQuality` 记录行数、日期数、资产数、空值比例、每日覆盖率中位数、全空日期、耗时秒数和 artifact 的 SHA-256。

运行： uv run python -m unittest tests.test_compute -v

预期：通过。

- [x] **步骤 3：验证确定性输出**

对相同输入运行两次，断言 plan、质量业务字段和 Parquet 内容哈希相同；耗时字段仅作为诊断信息保存。

- [x] **步骤 4：提交**

运行： git add src/factor_miner/compute.py tests/test_compute.py

提交：`git commit -m "feat: compute deterministic raw factor artifacts"`。

#### 任务 9 补充：评价诊断指标（已完成）

- [x] 记录 RankIC 均值、样本标准差、ICIR、正/负/零 RankIC 日期占比、q05/q25/q50/q75/q95 分位数和年度摘要。
- [x] 记录每日有效股票数的最小值/中位数/最大值，以及因子覆盖率、标签覆盖率和联合有效覆盖率。
- [x] 将诊断指标纳入 `EvaluationMetrics`、`RunResult` 和 CLI JSON 输出；这些描述性指标不改变现有 visible 准入闸门。

### 任务 9：可见 RankIC、HAC 与 Bonferroni

**文件：**

- 创建：`src/factor_miner/evaluation.py`
- 创建：`src/factor_miner/statistics.py`
- 创建：`tests/test_evaluation.py`
- 创建：`tests/test_statistics.py`

**接口：**

- 产出：`DailyRankIC`、`EvaluationMetrics`、`evaluate_rank_ic()`、`HacInference`、`hac_mean_test()`、`bonferroni_adjust()`。
- 使用：`CampaignSpec`、原始因子 artifact 和 rank mask。

#### Subagent 协作与人工闸门

本环节只定义可审计的协作协议，不接入 live LLM，不把 Subagent 输出直接视为研究结论。Subagent 输出必须是可保存、可复核的文本或 JSON；真实数据、完整结果和公司路径不得发送给外部模型。

1. **Subagent 1：经济学假设草稿。** 只生成 claim、mechanism、expected sign、observable proxy、独立验证方案、竞争解释、failure modes、falsification path 和来源线索；不得生成表达式或读取任何结果。
2. **人工筛选与冻结。** 人工逐条筛选假设，补齐并核验引用；未通过人工筛选的假设不得进入候选登记。通过后生成不可变 `HypothesisSpec`，并固定 `mechanism_unverified` 状态。
3. **Subagent 2：数学表达式草稿。** 只能基于已筛选且已冻结的假设生成符合白名单的 typed AST JSON；不得生成任意 Python、动态算子、标签字段或未来引用，也不得覆盖候选 ID、campaign、评价参数。
4. **自动化语法检查。** 在公司服务器上执行 schema、DSL、lookback、编译计划和真实数据合同检查；检查失败不得读取 label、计算 IC 或产生 outcome。
5. **Subagent 3：表达式一致性审查。** 输入冻结的假设和已通过语法检查的表达式，只审查表达式是否对应假设中的 observable proxy、方向和可得性；不得重新审查或改写假设本身。审查结果单独记录，失败时保留候选和 trial，不进入 visible 统计。
6. **批量统计闸门。** 只有人工筛选、语法检查和一致性审查都通过的已登记候选，才能进入批量 RankIC、HAC 和 Bonferroni。DSR 若后续加入，只能作为补充诊断，不能替代 Bonferroni family gate；sealed OOS 属于后续服务器阶段，不在本 Task 实现。

- [x] **前置步骤：冻结 Subagent 协作、人工筛选和表达式一致性审查边界**

- [x] **步骤 1：编写统计黄金测试**

创建完美正排名、完美负排名、常数因子、股票数不足、标签缺失和随机标签 null 的确定性面板。手工检查每日 Spearman 值。验证：

~~~python
self.assertEqual(bonferroni_adjust(0.001, 100), 0.1)
self.assertEqual(bonferroni_adjust(0.02, 100), 1.0)
~~~

检查 `hac_mean_test` 使用 campaign 的 max lags，并报告 nobs、均值、标准误、t 值、双侧 raw p 和置信区间。

运行： uv run python -m unittest tests.test_evaluation tests.test_statistics -v

预期：由于两个模块都不存在，测试失败。

- [x] **步骤 2：实现每日 RankIC**

过滤 valid_for_factor_rank、有限因子值和有限标签值。要求满足 min_names_per_date。逐日计算 Spearman，不填充缺失值；常数输入产生无效日期，而不是零 IC。强制使用 campaign 可见日期，并返回完整的有效/无效日期数和覆盖率摘要。

- [x] **步骤 3：实现 HAC 和准入统计**

使用 statsmodels 对 RankIC 序列和常数项执行 OLS，设置 `cov_type="HAC"`，并在 `cov_kwds` 中使用来自 `CampaignSpec` 的 maxlags。使用双侧 p 值。Bonferroni family size 必须精确等于 max_hypotheses，绝不能使用剩余候选数量。

当有效日期少于 min_valid_dates，或 campaign budget 未冻结时，拒绝进行推断。

- [x] **步骤 4：验证并提交**

运行： uv run python -m unittest tests.test_evaluation tests.test_statistics -v

预期：通过。

运行： git add src/factor_miner/evaluation.py src/factor_miner/statistics.py tests/test_evaluation.py tests/test_statistics.py

提交：`git commit -m "feat: evaluate visible RankIC with corrected inference"`。

### 任务 10：结构和输出冗余闸门

**文件：**

- 创建：`src/factor_miner/redundancy.py`
- 创建：`tests/test_redundancy.py`

**接口：**

- 产出：`StructuralRedundancy`、`OutputRedundancy`、`check_structural_redundancy()`、`check_output_redundancy()`。
- 使用：规范化 AST 元数据、原始因子面板和 `CampaignSpec` 阈值。

- [x] **步骤 1：编写冗余测试**

测试精确规范化重复、交换律重复、不同窗口、不同 AST 产生相同输出、高正/负输出相关性、无关输出和重叠股票数不足。必须包含总体和分年度相关性摘要。

运行：`uv run python -m unittest tests.test_redundancy -v`

预期：由于 `redundancy.py` 不存在，测试失败。

- [x] **步骤 2：实现两个阶段**

结构阶段将规范化哈希以及字段/窗口/算子签名，与 campaign 候选和配置的 reference pool 比较。输出阶段在有效股票重叠部分逐日计算截面 Spearman，然后报告相关系数中位数、绝对相关系数 95 分位数、分年度中位数和重叠数量。

候选只能与冻结的 reference pool 以及 `CampaignSpec` 中排在它之前的候选比较。当冻结策略选定的任一绝对相关摘要超过 max_abs_output_correlation 时失败。看到结果后不得删除候选、重新排序或选择代表候选。

- [x] **步骤 3：验证并提交**

运行：`uv run python -m unittest tests.test_dsl tests.test_redundancy -v`

预期：通过。

运行：`git add src/factor_miner/redundancy.py tests/test_redundancy.py`

提交：`git commit -m "feat: gate structural and output factor redundancy"`。

### 任务 11：队列工作流与 CLI

**文件：**

- 创建：`src/factor_miner/workflow.py`
- 创建：`src/factor_miner/cli.py`
- 创建：`tests/test_workflow.py`
- 创建：`tests/test_cli.py`

**接口：**

- 产出：`CandidateTerminalStatus`、`RunResult`、`run_smoke_campaign()`、`run_visible_campaign()`，以及 Typer 命令 `doctor`、`validate-spec`、`register-candidate`、`register-campaign`、`compile-spec`、`run-smoke`、`run-visible`、`ledger-verify`。
- 使用：此前的全部模块。

- [x] **步骤 1：编写工作流状态测试**

验证事件顺序为 `candidate_registered → campaign_registered → run_started → outcome_exposed → evaluation_completed → visible_passed` 或明确失败。Campaign 登记必须先于 `DataSource.scan`。编译失败绝不能写入 outcome_exposed；读取标签后的计算/评价失败必须写入 outcome_exposed。

验证候选终态包始终包含 `visible_only=true`、`sealed_oos_used=false`、`production_eligible=false`，且 mechanism_status 与假设中的值保持一致。

- [x] **步骤 2：编写 CLI 防护测试**

使用 Typer `CliRunner`。测试 help/validate 成功、非法 spec 返回非零、Darwin profile 下 visible run 返回非零、损坏账本返回非零，并确认不存在名为 ignore-state、use-local-data、skip-ledger、force-test、dolphindb 或 postgres 的参数。

运行： uv run python -m unittest tests.test_workflow tests.test_cli -v

预期：由于工作流和 CLI 不存在，测试失败。

- [x] **步骤 3：实现工作流**

每次候选评价使用一个 run ID。在第一个结果产生前登记完整 campaign。将 artifacts 写入临时 run 目录，对文件执行 fsync，原子重命名目录，然后追加包含全部 artifact 哈希的终态事件。发生异常时追加匹配的失败事件并重新抛出 `FactorMinerError`；绝不静默进入下一阶段。

- [x] **步骤 4：实现 CLI**

每个命令向 stdout 输出紧凑 JSON，向 stderr 输出诊断信息。doctor 校验运行时、数据合同和账本，但不计算因子。`RuntimeProfile` 在单一 artifact root 下派生 state 和 runs 目录。run-smoke 计算原始因子并检查确定性，不执行显著性或可见准入。run-visible 接收已登记的 campaign ID，不接受来自候选输入的评价器覆盖参数。

CLI 现阶段同时输出质量、RankIC、HAC、Bonferroni，以及 ICIR、方向占比、分位数、年度、样本数和因子/标签覆盖率诊断；这些诊断不改变本阶段准入规则。

- [x] **步骤 5：验证并提交**

运行： uv run python -m unittest discover -s tests -v

预期：通过。

运行： git add src/factor_miner/workflow.py src/factor_miner/cli.py tests/test_workflow.py tests/test_cli.py

提交：`git commit -m "feat: orchestrate auditable visible factor campaigns"`。

### 任务 12：全合成端到端验收

**文件：**

- 创建：`examples/candidates/momentum_20d.json`
- 创建：`examples/candidates/negative_shift_invalid.json`
- 创建：`examples/campaigns/synthetic_visible.json`
- 创建：`tests/test_synthetic_e2e.py`
- 修改：`README.md`

**接口：**

- 产出：一条可复现的合成 E2E 命令和验收 artifact 目录布局。

- [x] **步骤 1：添加程序生成的集成数据**

测试创建至少 120 个日期和 150 个合成资产，包含带固定随机种子的潜在信号、类价格字段、mask 和未来标签。不得读取任何仓库文件或用户数据文件。

- [x] **步骤 2：验证成功和失败路径**

在冻结 campaign 中运行一个预期方向明确且信号强的候选、一个打乱标签的候选、一个 negative-shift 候选、一个全空候选和两个规范化重复候选。断言只有预期的强信号非重复候选达到 visible_passed，其余尝试都具有预期的错误/状态。

- [x] **步骤 3：验证重启和审计**

运行账本 verify，以幂等方式重放同一个不可变候选，检测手工损坏的副本，并验证没有 artifact 路径进入仓库或 QuantLake。

运行： `uv run python -m unittest tests.test_synthetic_e2e -v`

预期：通过。

- [x] **步骤 4：运行完整测试套件并提交**

运行： uv run python -m unittest discover -s tests -v

预期：通过，且真实数据读取次数为零。

运行：`git add examples/candidates/momentum_20d.json examples/candidates/negative_shift_invalid.json examples/campaigns/synthetic_visible.json tests/test_synthetic_e2e.py README.md`

提交：`git commit -m "test: add synthetic end-to-end acceptance"`。

提交：`git commit -m "test: prove synthetic factor miner V0 workflow"`。

### V1 设计预留：候选池与树模型 TreeSHAP 递归筛选

V1 再实现“LLM 生成候选池 → 树模型 → TreeSHAP → 递归筛选”的模型层流程，V0 不接入 live LLM、树模型或 `shap` 依赖。V1 的初始实现也必须允许使用人工或录制的候选池，以便在没有 live LLM 时复现相同协议。

#### V1 数据接入预留：Smoke 输入与正式 QuantLake 读取分离

1. `/data/factor_miner_artifacts/inputs` 下的文件只用于固定日期、固定资产集合的有界 Smoke 验收，不得被正式 visible 或 production run 当作全量数据源。当前 V0 的真实行情验收属于工程接线验证，不代表全量研究结论。
2. 正式运行必须通过服务器私有配置提供经过 release 解析的显式 URI：行情和状态直接读取 `/data/quantlake` 的只读 release；适配器使用 `scan_parquet`、字段投影和日期过滤，禁止把 QuantLake 全量复制到 artifact 根目录。
3. 标签必须使用版本化、可审计的服务器侧派生产物，或者使用后续实现的服务器标签适配器按冻结规则从 QuantLake 派生。标签定义、可得日、截止日期、未来窗口、复权口径和来源 release 必须在读取结果前冻结；不得使用 Mac 数据、隐藏 fallback 或运行时导入 `huan_quant`。
4. `smoke` 与 `visible/production` 必须使用不同的运行配置并进行路径闸门：Smoke 可以指向 artifact 根目录下的有界输入；正式运行不得指向该目录，输出只能写入独立 artifact 根目录。每次正式 run 仍须记录 data origin、release、manifest hash、schema、cutoff、状态表版本、代码提交、配置哈希和 `uv.lock` 哈希。

冻结的候选池与筛选协议如下：

1. LLM 或人工先生成约 1000 个候选 `CandidateFactorSpec`；每个候选在读取任何标签、IC 或 SHAP 结果前登记，占用预先声明的 hypothesis slot。人工筛选、typed AST 校验、可得性检查和独立假设字段必须先完成。
2. 树模型只使用冻结训练集拟合；训练/筛选/最终验证区间、股票池、标签、缺失值处理、模型家族、超参数、随机种子、早停规则和 SHAP 版本必须在结果产生前写入 `ModelCampaignSpec`。最终验证区间不得参与候选删除或 Top100 选择。
3. 计算每个候选的 `Mean(|SHAP|)`，在结果产生前冻结“接近零”的判定阈值、聚合样本范围和最小覆盖率。删除的候选必须保留登记、失败/筛选事件和统计 family 位置，不得从账本或多重检验分母中消失。
4. 第一轮只保留预登记的 Top100；重新训练次数、每轮候选数、停止条件和并列处理规则必须冻结。每一轮都新增模型选择事件和完整候选映射，不能原地覆盖上一轮结果。
5. TreeSHAP 只能作为模型层的候选筛选和解释诊断，不能替代 RankIC、HAC、Bonferroni、冗余检查或独立机制验证。递归筛选结束后仍须对保留候选执行因子层 visible validation，并将最终选择偏差纳入 V1 报告。

### 任务 13：公司服务器初始化与有界真实数据 Smoke

**执行环境：**仅限公司服务器。本任务需要明确批准的私有 Git 远端或其他经用户批准的 Git 传输方式；任何代理都不得将公司敏感代码推送到未批准的个人远端。

服务器已有 `work` 和 `Test` 两个环境。本任务直接使用其中经批准的现有环境，不创建新的虚拟环境、conda 环境或项目环境；服务器连接账号、密码和私有路径只在会话或服务器私有配置中使用，不写入 Git。

**文件：**

- 在 Git 外创建：`~/.config/factor_miner/company_a_share.env`
- 在 Git 外产出：`FM_ARTIFACT_ROOT` 下的 state 和 artifacts

- [x] **步骤 1：建立正式服务器检出目录**

将批准的仓库克隆到 `~/work/factor_miner`，验证 git status 干净，并验证检出的 commit 与 Mac 审查过的 commit 一致。不得使用临时版本目录复制代码。

- [x] **步骤 2：校验现有锁定环境**

选择经批准的 `work` 或 `Test` 环境，校验 Python 版本和 `uv.lock` 中的依赖版本。不得运行会创建新 `.venv` 的环境初始化命令；若现有环境不满足锁定依赖，先停止并报告，不擅自创建环境。

预期：现有环境为 Python 3.12，且依赖与 `uv.lock` 一致。

- [x] **步骤 3：创建服务器私有配置**

将 `configs/company_a_share.env.example` 中的变量名复制到 `~/.config/factor_miner/company_a_share.env`。从服务器批准的不可变 release 中解析 market/state/label URI，并记录实际 release、manifest、复权、calendar 和 state 版本。将 artifact root 设置为 `/data/factor_miner_artifacts`（与 `/data/quantlake` 同级、位于 `/data` 下且独立可写）。文件权限保持为 600。

- [x] **步骤 4：在任何因子计算前运行 doctor**

加载私有环境，然后在已有 `work` 环境中运行：`factor-miner doctor`

预期：返回 JSON status ok，确认 Linux 运行时、QuantLake 只读、独立 artifact root 可写、cutoff 一致、mask 合法且 provenance 完整。任何失败都会阻断本任务。

- [x] **步骤 5：运行有界真实数据 Smoke**

使用固定的小日期区间、固定的小资产列表和已登记的动量示例。登记 Smoke campaign，将 `FM_SMOKE_CAMPAIGN_ID` 设置为 register-campaign 打印的准确 ID，然后运行：

factor-miner run-smoke --campaign-id "$FM_SMOKE_CAMPAIGN_ID"

Smoke 只验证 schema、连接、warmup、原始输出和确定性；不得报告 alpha 显著性或 visible_passed。

运行完全相同的 Smoke 两次，并要求 plan/raw factor 哈希完全一致。

- [x] **步骤 6：验证只读边界和审计状态**

确认 QuantLake 的 mtime/内容没有改变，账本 verify 通过，所有 run artifacts 位于 `FM_ARTIFACT_ROOT` 下，且 git status 中没有真实 artifact。

- [x] **步骤 7：只提交代码或文档修正**

如果 Smoke 暴露缺陷，必须先用失败的合成回归测试固定根因，再重新运行完整测试套件和 Smoke，最后提交。绝不提交服务器私有配置、账本或 artifacts。

### 任务 14：第一次冻结的可见 Campaign

**执行环境：**仅限公司服务器。登记前必须获得人工对 CampaignSpec 的明确批准。

**文件：**

- 在 Git 外产出：不可变 CampaignSpec、候选文档、账本和 run artifacts。
- 仅在协议含义发生变化时修改：`docs/constraints` 或 `docs/contracts`。

- [x] **步骤 1：起草有界 CampaignSpec**

使用小规模、预先声明的候选集合。在读取结果前冻结可见日期、candidate ID、max_hypotheses、alpha=0.05、label_o2o_5d、valid_for_factor_rank、HAC 最大滞后阶数、最小覆盖率/日期/股票数阈值、输出相关性阈值和 reference pool。

- [x] **步骤 2：取得人工批准并只登记一次**

将批准的草稿写入 `"$FM_ARTIFACT_ROOT/drafts/first_visible_campaign.json"`。展示规范化 CampaignSpec 和哈希。批准后运行：

factor-miner register-campaign "$FM_ARTIFACT_ROOT/drafts/first_visible_campaign.json"

将 `FM_VISIBLE_CAMPAIGN_ID` 设置为命令打印的准确 ID。任何改动都会生成新的 campaign ID；如果相同规范化内容已经在 Smoke 阶段登记，则复用原 ID，不重复登记。运行前验证所有候选 slot 都已登记。

- [x] **步骤 3：运行 Campaign**

运行： factor-miner run-visible --campaign-id "$FM_VISIBLE_CAMPAIGN_ID"

预期：每个候选都达到一个明确终态；账本没有缺失 trial，Bonferroni family size 等于预登记的 max_hypotheses。

- [x] **步骤 4：审查输出但不晋级**

报告 generated、compiled、outcome-exposed、quality-failed、significance-failed、redundancy-failed 和 visible-passed 的数量。对每个可见候选报告 mean RankIC、HAC raw p、Bonferroni p、coverage、总体/年度冗余和 mechanism_status。

不得打开 sealed OOS，不得回测，不得声称 Evidence，也不得将任何因子导入 `huan_quant`。

- [x] **步骤 5：收尾 V0**

运行： factor-miner ledger-verify

运行： python -m unittest discover -s tests -v

预期：两项都通过。在 run manifest 中记录最终代码 commit、`uv.lock` 哈希、campaign 哈希和数据 provenance。只有所有前置任务都已勾选且可见 Campaign 完整可审计时，才能将本实施计划标记为完成。
