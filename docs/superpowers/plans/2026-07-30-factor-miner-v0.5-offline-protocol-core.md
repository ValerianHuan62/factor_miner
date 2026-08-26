# Factor Miner V0.5 离线协议核心实施计划

> **代理执行要求：** 必须使用 `superpowers:executing-plans` 逐项实施本计划。步骤使用复选框（`- [ ]`）跟踪。本仓库继续禁止使用开发子代理；设计中的三个代理是未来运行时角色，不是本计划的编码代理。

**目标：** 在不连接 DeepSeek、文献网络或真实行情的前提下，实现 V0.5 的不可变合同、严格时间留出判定、脱敏覆盖摘要、120 槽状态机、不可逆生成封存和结果防火墙。

**架构：** 新增相互独立的 V0.5 Schema、覆盖摘要、状态投影和账本模块；复用现有规范 JSON、内容寻址、单写入者锁、哈希链、fsync 与原子写入不变量。离线 CLI 只登记、构建合成摘要、追加状态、封存和核验，不包含外部模型客户端、文献适配器、真实评价器或 outcome 读取能力。

**技术栈：** Python 3.12、Pydantic 2、Typer、标准库、unittest、规范 JSON/JSONL。

## 全局约束

- 所有新增和修改的 Markdown、代码注释与文档字符串使用中文。
- Mac 只运行程序生成的纯合成测试；公司真实数据、真实图谱、候选评价和统计推断仍只能在公司 Linux 执行。
- 本计划不得增加 HTTP 客户端、DeepSeek SDK、浏览器、文献网络、API Key 读取或真实 outcome 端口。
- 不得把 `huan_quant`、个人 Skill、个人目录或个人数据库变成运行时依赖。
- 所有 V0.5 Pydantic 模型必须 `frozen=True, extra="forbid"`；规范 JSON 与内容哈希沿用 `factor_miner.canonical`。
- discovery family 固定四个 arm、两个 LLM campaign、每 arm 30 个候选槽和全局 120 个统计名额。
- 所有 120 个槽必须进入不可变生成终态后才能生成 `generation_sealed` 事件；seal 后禁止新增、替换、删除或重置槽。
- `strict_temporal_holdout` 的硬条件使用全部已封存候选的原始依赖区间：评价所依赖的最早原始观测必须严格晚于 discovery context 的最晚信息时点。
- 时间留出校验必须同时覆盖因子回看输入和五日标签所依赖的原始交易日，不能只比较候选生成时间、形成日期或评价标签日期。
- 数据发生任何重叠时，预登记为 `strict_temporal_holdout` 的 family 必须硬失败；只有事前登记的 `reused_discovery_data` 才能继续，并且只允许探索性结果命名。
- 本计划不运行真实 120 槽 campaign，不声明候选有效、机制成立或完成选择偏差校正。

---

### 任务 1：冻结发现研究族、数据身份与严格时间留出合同

**文件：**

- 新建：`src/factor_miner/llm_schema.py`
- 修改：`src/factor_miner/errors.py`
- 新建：`tests/test_llm_schema.py`

**接口：**

- 产出：`DiscoveryDataIdentity`、`CandidateEvaluationDataIdentity`、`EvaluationDependencyInterval`、`EvaluationRelationship`、`DiscoveryArmSpec`、`LLMDiscoveryResearchFamilySpec`、`RegisteredLLMDiscoveryResearchFamily`、`registered_llm_discovery_family()`、`validate_evaluation_relationship()`。
- 依赖：`factor_miner.canonical.sha256_json` 和现有 `EvaluationPolicySpec` 的内容身份。

- [x] **步骤 1：编写内容寻址与固定预算失败测试**

在 `tests/test_llm_schema.py` 构造四个固定 arm，断言合法 family 生成稳定 `llmfamily_<24 hex>`；将 `global_statistical_trial_budget` 改成 119、删除任一 arm、把 LLM campaign 数改成 4、把单 arm 槽数改成 29 时均触发 `ValidationError`。

```python
def test_family_freezes_four_arms_and_120_slots(self) -> None:
    registered = registered_llm_discovery_family(valid_family_spec())
    self.assertRegex(registered.discovery_family_id, r"^llmfamily_[0-9a-f]{24}$")
    self.assertEqual(
        sum(arm.candidate_slots for arm in registered.spec.arm_specs),
        120,
    )

def test_family_rejects_budget_that_does_not_match_slots(self) -> None:
    payload = valid_family_spec().model_dump()
    payload["global_statistical_trial_budget"] = 119
    with self.assertRaises(ValidationError):
        LLMDiscoveryResearchFamilySpec.model_validate(payload)
```

- [x] **步骤 2：运行测试并确认失败**

运行：

```bash
uv run python -m unittest tests.test_llm_schema -v
```

预期：导入失败，因为 `factor_miner.llm_schema` 尚不存在。

- [x] **步骤 3：实现不可变 family 与数据身份模型**

`DiscoveryDataIdentity` 至少包含：

```python
coverage_graph_ids: tuple[str, ...]
data_release_id: str
universe_id: str
evaluation_policy_id: str
label_id: str
visible_start: date
visible_end: date
latest_information_timestamp_used: datetime
source_manifest_sha256: str
```

`CandidateEvaluationDataIdentity` 至少包含相同发布和评价身份、计划评价区间及 `source_manifest_sha256`。四个 arm ID 固定为 `coverage_outcome_llm`、`literature_only_llm`、`mechanical_mutation`、`hypothesis_conditioned_grammar`；`maximum_llm_campaigns=2`、`arm_run_count=4`、每 arm 30 槽、总预算 120。注册函数使用：

```python
payload = spec.model_dump(mode="json")
spec_hash = sha256_json(payload)
return RegisteredLLMDiscoveryResearchFamily(
    discovery_family_id=f"llmfamily_{spec_hash[:24]}",
    spec_hash=spec_hash,
    spec=spec,
)
```

- [x] **步骤 4：编写严格时间留出边界测试**

增加三个必须区分的合成案例：

```python
def test_strict_holdout_uses_earliest_raw_observation_not_generation_time(self):
    discovery = identity(latest_information="2026-01-08T00:00:00+08:00")
    interval = dependency(
        candidate_slot_id="coverage_outcome_llm:001",
        factor_input_start="2026-01-08",
        factor_input_end="2026-02-01",
        label_input_start="2026-02-02",
        label_input_end="2026-02-06",
        candidate_generated_at="2026-02-20T00:00:00+08:00",
    )
    with self.assertRaisesRegex(ValueError, "最早原始观测"):
        validate_evaluation_relationship(
            EvaluationRelationship.STRICT_TEMPORAL_HOLDOUT,
            discovery,
            (interval,),
        )

def test_discovery_five_day_label_crossing_boundary_rejects_holdout(self):
    discovery = identity(
        visible_end="2025-12-31",
        latest_information="2026-01-08T00:00:00+08:00",
    )
    interval = dependency(
        factor_input_start="2026-01-05",
        factor_input_end="2026-02-01",
        label_input_start="2026-02-02",
        label_input_end="2026-02-06",
    )
    with self.assertRaisesRegex(ValueError, "2026-01-05"):
        validate_evaluation_relationship(
            EvaluationRelationship.STRICT_TEMPORAL_HOLDOUT,
            discovery,
            (interval,),
        )

def test_strict_holdout_accepts_only_raw_observations_after_discovery_information(self):
    discovery = identity(latest_information="2026-01-08T00:00:00+08:00")
    interval = dependency(
        factor_input_start="2026-01-09",
        factor_input_end="2026-02-01",
        label_input_start="2026-02-02",
        label_input_end="2026-02-06",
    )
    validate_evaluation_relationship(
        EvaluationRelationship.STRICT_TEMPORAL_HOLDOUT,
        discovery,
        (interval,),
    )
```

`EvaluationDependencyInterval` 自己校验四个日期有序，并将
`earliest_raw_observation=min(factor_input_start, label_input_start)`。
严格模式比较所有有效候选的最小值与 discovery 的
`latest_information_timestamp_used`；比较前统一到上海时区的交易日边界。
`reused_discovery_data` 允许重叠，但返回固定
`evidence_tier="exploratory_filter_only"`。

- [x] **步骤 5：运行任务测试并提交**

运行：

```bash
uv run python -m unittest tests.test_llm_schema -v
```

预期：全部通过。

提交：

```bash
git add src/factor_miner/llm_schema.py src/factor_miner/errors.py tests/test_llm_schema.py
git commit -m "feat: freeze v0.5 discovery family contracts"
```

### 任务 2：实现脱敏覆盖摘要与确定性 gap 选择

**文件：**

- 新建：`src/factor_miner/llm_brief.py`
- 新建：`src/factor_miner/llm_privacy.py`
- 新建：`tests/test_llm_brief.py`
- 新建：`tests/test_llm_privacy.py`

**接口：**

- 消费：任务 1 的 discovery data identity；V0.4 `CoverageGraphSnapshot` 的纯内存合成等价物。
- 产出：`GapTaxonomyUnit`、`GapSelectionPolicy`、`CoverageGapCard`、`LLMCoverageBriefSpec`、`LLMCoverageBrief`、`select_coverage_gaps()`、`scan_export_payload()`、`build_export_preview()`。

- [x] **步骤 1：编写 gap 排名与动态配额失败测试**

构造至少 12 个乱序 gap，覆盖三个 category、`0/1/2_4` 结构分箱、`0/1/2_4` 信号分箱和 `none/low/medium/high` 风险。断言：

- `high` 在排序前删除；
- 第一轮按 canonical category ID 每类取一个；
- 第二轮每次选择后重新计算 allocation；
- 每类最多两个；
- 输入顺序打乱不改变结果；
- hash 只作为最终稳定并列键。

```python
def test_gap_selection_is_order_independent_and_recomputes_quota(self) -> None:
    forward = select_coverage_gaps(tuple(gaps()), GapSelectionPolicy())
    reverse = select_coverage_gaps(tuple(reversed(gaps())), GapSelectionPolicy())
    self.assertEqual(forward, reverse)
    self.assertLessEqual(
        max(Counter(card.category for card in forward).values()),
        2,
    )
```

- [x] **步骤 2：运行测试并确认失败**

运行：

```bash
uv run python -m unittest tests.test_llm_brief tests.test_llm_privacy -v
```

预期：导入失败，因为 brief 与 privacy 模块尚不存在。

- [x] **步骤 3：实现冻结排名与内容寻址 brief**

在模块常量中固定：

```python
COUNT_BAND_RANK = {"0": 0, "1": 1, "2_4": 2}
FAILURE_RISK_RANK = {"none": 0, "low": 1, "medium": 2}
```

先按 category 升序完成一类一卡，再按
`(structure_rank, signal_rank, failure_rank, current_allocation,
category_order, canonical_gap_hash)` 动态重排，最多选择 10 张。
`LLMCoverageBrief` 只接受公开字段别名、分箱值和局部匿名 ID，
并用规范 payload 生成 `brief_<24 hex>`。

- [x] **步骤 4：编写绝对禁止与政策条件允许测试**

测试 payload 中出现以下任一内容都会抛出
`FailureCode.LLM_PRIVACY_VIOLATION`：绝对路径、IPv4、邮箱、
Bearer/API Key 样式、高熵密钥、真实因子 ID、未声明字段。
测试 coverage gap、失败类型和表现分箱只有在
`allowed_information_classes` 明确包含相应类别时才能进入预览。

```python
def test_policy_cannot_authorize_absolutely_forbidden_path(self) -> None:
    payload = {"summary": "/data/quantlake/private.parquet"}
    with self.assertRaises(FactorMinerError) as context:
        scan_export_payload(payload, permissive_policy())
    self.assertEqual(
        context.exception.code,
        FailureCode.LLM_PRIVACY_VIOLATION,
    )
```

- [x] **步骤 5：实现白名单序列化后扫描**

扫描规范 UTF-8 JSON 字节，不对失败内容做自动删改。实现固定正则和
Unicode 控制字符检查；高熵检查只针对长度至少 24 的连续 token，
避免普通中文文本误报。`build_export_preview()` 返回 payload SHA-256、
字段计数和信息类别，不返回私有禁词路径或真实图谱 ID。

- [x] **步骤 6：运行任务测试并提交**

运行：

```bash
uv run python -m unittest tests.test_llm_brief tests.test_llm_privacy -v
```

预期：全部通过。

提交：

```bash
git add src/factor_miner/llm_brief.py src/factor_miner/llm_privacy.py tests/test_llm_brief.py tests/test_llm_privacy.py
git commit -m "feat: build deterministic private llm briefs"
```

### 任务 3：冻结假设、预测合成与文献用途合同

**文件：**

- 新建：`src/factor_miner/llm_hypothesis.py`
- 新建：`tests/test_llm_hypothesis.py`

**接口：**

- 消费：任务 1 的 family ID、现有 `EvaluationPolicySpec`。
- 产出：`PredictionProposal`、`TestablePredictionSpec`、`CoverageGapHypothesisDraft`、`CitationSupportRecord`、`HypothesisLiteratureStatus`、`HypothesisDecision`、`RegisteredCoverageGapHypothesis`、`compose_testable_prediction()`、`summarize_hypothesis_candidates()`。

- [x] **步骤 1：编写 LLM 不能覆盖评价政策的失败测试**

`PredictionProposal` 只允许 proxy、方向、字段别名、算子族和可选条件主张。
传入 `alpha`、`universe_id`、`horizon_sessions` 或
`multiplicity_family_id` 必须因 `extra="forbid"` 失败。
`compose_testable_prediction()` 必须从已冻结 policy/family 注入这些值。

```python
def test_prediction_proposal_rejects_locked_evaluation_fields(self) -> None:
    payload = valid_prediction_proposal().model_dump()
    payload["alpha"] = 0.50
    with self.assertRaises(ValidationError):
        PredictionProposal.model_validate(payload)
```

- [x] **步骤 2：运行测试并确认失败**

运行：

```bash
uv run python -m unittest tests.test_llm_hypothesis -v
```

预期：导入失败，因为 hypothesis 模块尚不存在。

- [x] **步骤 3：实现预测合成和来源四分类**

`CitationSupportRecord.support_kind` 只允许
`mechanism_claim_supported`、`proxy_choice_supported`、
`background_only`。假设状态按最强已核验记录汇总；
只有人工事件能把 `background_only` 转成 `novel_unverified`，
且每个 10 槽 LLM arm 最多两个。

反证记录必须同时包含：

```python
counterevidence_search_performed: bool
counterevidence_queries: tuple[str, ...]
counterevidence_source_ids: tuple[str, ...]
counterevidence_cutoff: date
counterevidence_summary: str
counterevidence_limitations: str
```

当 `counterevidence_search_performed=True` 而查询、截止日或局限为空时拒绝。

- [x] **步骤 4：实现候选结果与假设摘要分层**

定义 strict 模式候选结果、exploratory 模式筛选结果和设计批准的五种
`HypothesisDiscoverySummary`。固定摘要优先级：

```python
if not valid:
    return NO_VALID_CANDIDATE
if outcomes == {INCONCLUSIVE}:
    return ALL_CANDIDATES_INCONCLUSIVE
if outcomes == {REVERSE}:
    return ALL_VALID_CANDIDATES_REVERSE
if REVERSE in outcomes:
    return MIXED_CANDIDATE_EVIDENCE
return HAS_SUPPORTED_CANDIDATE
```

探索性结果不得映射成 `HAS_SUPPORTED_CANDIDATE`。

- [x] **步骤 5：运行任务测试并提交**

运行：

```bash
uv run python -m unittest tests.test_llm_hypothesis -v
```

预期：全部通过。

提交：

```bash
git add src/factor_miner/llm_hypothesis.py tests/test_llm_hypothesis.py
git commit -m "feat: freeze v0.5 hypothesis contracts"
```

### 任务 4：实现槽位状态机与 family 投影

**文件：**

- 新建：`src/factor_miner/llm_state.py`
- 新建：`tests/test_llm_state.py`

**接口：**

- 消费：任务 1 的 registered family、任务 3 的 hypothesis/candidate 状态。
- 产出：`LLMDiscoveryEvent`、`LLMDiscoveryFamilyState`、`CampaignOperationalState`、`CampaignQualityAssessment`、`HypothesisSlotState`、`CandidateSlotState`、`LogicalCallState`、`LiteratureQueryState`、`apply_discovery_event()`、`project_discovery_family_state()`。

- [x] **步骤 1：编写非法转换与终态不可逆测试**

覆盖以下硬规则：

- candidate 不能从 `reserved` 直接到 `ready_for_registration`；
- `generation_failed` 和两个 `not_executed_*` 是终态；
- hypothesis `human_rejected` 后不能变回 `human_approved`；
- campaign operational 状态与 quality assessment 可以正交组合；
- 事件引用不存在的 slot、campaign 或 arm run 时失败；
- superseding 事件只能更正允许更正的元数据，不能逆转研究终态。

- [x] **步骤 2：运行测试并确认失败**

运行：

```bash
uv run python -m unittest tests.test_llm_state -v
```

预期：导入失败，因为状态模块尚不存在。

- [x] **步骤 3：用显式允许边实现纯函数投影**

为每种对象维护不可变 `dict[state, frozenset[next_state]]`。
`apply_discovery_event(state, event)` 不读文件、不取当前时间、不访问环境；
相同初始状态与事件序列必须得到逐字节相同投影。
family 状态只允许：

```python
REGISTERED -> GENERATING -> GENERATION_SEALED -> EVALUATION_OPEN -> EVALUATED
```

`family_closed_at` 只来自事件时间，不存在于 family spec。

- [x] **步骤 4：编写 120 槽完整性测试**

从 family spec 生成固定槽 ID：
`<arm_id>:001` 至 `<arm_id>:030`。断言重复、缺失、额外槽、
错 arm 和错编号全部失败。覆盖“coverage 假设被拒绝后，
mechanical/grammar 对应槽进入 `not_executed_hypothesis_rejected`”。

- [x] **步骤 5：运行任务测试并提交**

运行：

```bash
uv run python -m unittest tests.test_llm_state -v
```

预期：全部通过。

提交：

```bash
git add src/factor_miner/llm_state.py tests/test_llm_state.py
git commit -m "feat: add irreversible v0.5 state projection"
```

### 任务 5：复用哈希链账本并登记 V0.5 不可变文档

**文件：**

- 修改：`src/factor_miner/ledger.py`
- 新建：`src/factor_miner/llm_ledger.py`
- 修改：`tests/test_ledger.py`
- 新建：`tests/test_llm_ledger.py`

**接口：**

- 消费：任务 1 至任务 4 的不可变模型。
- 产出：可复用的内部 `_HashChainJsonlStore`、`LLMDiscoveryLedger`、family/campaign/arm/brief/hypothesis 文档登记、`append_event()`、`verify()`、`project_state()`。

- [x] **步骤 1：先锁定现有 Trial 账本字节行为**

在 `tests/test_ledger.py` 增加回归测试，固定两条已知事件的 sequence、
previous hash 和 verify 结果。该测试在重构前必须通过：

```bash
uv run python -m unittest tests.test_ledger -v
```

- [x] **步骤 2：编写 V0.5 文档与独立事件链失败测试**

断言：

- 相同 family/brief/campaign/arm 文档幂等；
- 同路径不同字节拒绝覆盖；
- `llm_events.jsonl` 截断、重复 ID、错 sequence、错 previous hash、
  错 event hash 和并发写入全部失败；
- V0.5 事件不能写入既有 `trials.jsonl`；
- projection 必须先 verify 完整哈希链。

- [x] **步骤 3：提取共享哈希链存储而不改变旧接口**

把锁、读取校验、sequence、previous hash、event hash、append、
flush/fsync 抽成 `ledger.py` 内部 `_HashChainJsonlStore`。
`JsonlLedger.append_event/read_events/verify` 继续接受和返回
`TrialEvent`，路径仍为 `trials.jsonl`，现有测试字节与行为不变。
`LLMDiscoveryLedger` 使用同一存储类但独立路径：

```text
state/llm_discovery_families/<family_id>/family_spec.json
state/llm_discovery_families/<family_id>/arm_specs/
state/llm_discovery_families/<family_id>/llm_events.jsonl
state/llm_briefs/<brief_id>/brief.json
```

- [x] **步骤 4：运行旧账本与新账本测试**

运行：

```bash
uv run python -m unittest tests.test_ledger tests.test_llm_ledger -v
```

预期：全部通过，旧 `JsonlLedger` API 无变化。

- [x] **步骤 5：提交**

```bash
git add src/factor_miner/ledger.py src/factor_miner/llm_ledger.py tests/test_ledger.py tests/test_llm_ledger.py
git commit -m "refactor: share immutable v0.5 ledger invariants"
```

### 任务 6：实现不可逆 family seal 与结果防火墙

**文件：**

- 新建：`src/factor_miner/llm_seal.py`
- 新建：`tests/test_llm_seal.py`

**接口：**

- 消费：任务 1 的 family/data identities、任务 4 的完整状态投影、
  任务 5 的已核验账本。
- 产出：`GenerationSealManifest`、`RegisteredGenerationSeal`、
  `build_generation_seal()`、`verify_generation_seal()`、
  `authorize_evaluation_open()`。

- [x] **步骤 1：编写 seal 前 outcome 防火墙测试**

使用注入的 `OutcomeProbe`，其 `open_count` 初始为零。分别构造 119 个
终态槽、一个非终态槽、manifest 缺失 candidate spec、状态投影未经
账本 verify 的情况，断言 `authorize_evaluation_open()` 失败且
`open_count == 0`。

```python
def test_outcome_port_stays_closed_until_all_slots_are_sealed(self) -> None:
    probe = OutcomeProbe()
    with self.assertRaises(FactorMinerError):
        authorize_evaluation_open(
            incomplete_family_state(),
            unsealed_manifest(),
            probe.open,
        )
    self.assertEqual(probe.open_count, 0)
```

- [x] **步骤 2：运行测试并确认失败**

运行：

```bash
uv run python -m unittest tests.test_llm_seal -v
```

预期：导入失败，因为 seal 模块尚不存在。

- [x] **步骤 3：生成并核验完整 manifest**

manifest 必须包含 family spec hash、四个 arm spec hash、两个 LLM
campaign spec hash、120 个槽的终态与对象 hash、prompt/model/generator
身份、两个数据身份、评价关系、评价 policy 和全局 120 预算。
缺失对象使用明确 terminal record hash，不能省略。
seal ID 使用 `llmseal_<24 hex>`，写入后不能覆盖。

- [x] **步骤 4：把严格时间留出校验放在打开结果之前**

`authorize_evaluation_open()` 的固定顺序：

```python
verify_generation_seal(seal, ledger_projection)
validate_evaluation_relationship(
    family.spec.evaluation_relationship,
    family.spec.discovery_context_data_identity,
    seal.manifest.evaluation_dependency_intervals,
)
append_evaluation_open_event()
return outcome_opener()
```

增加故障测试证明：

- 候选生成时间在 cutoff 后，但因子 lookback 原始输入跨过 cutoff 时失败；
- discovery 的 5 日标签使 `latest_information_timestamp_used`
  晚于 `visible_end` 时，评价输入落在两者之间会失败；
- 所有因子输入和标签输入最早原始观测严格晚于 cutoff 时才打开一次；
- `reused_discovery_data` 可以打开探索性端口，但返回
  `exploratory_filter_only`，不能请求 strict outcome Schema；
- seal 后追加、替换或删除槽导致 hash 不一致并硬失败。

- [x] **步骤 5：运行任务测试并提交**

运行：

```bash
uv run python -m unittest tests.test_llm_seal -v
```

预期：全部通过。

提交：

```bash
git add src/factor_miner/llm_seal.py tests/test_llm_seal.py
git commit -m "feat: seal generation before any outcome access"
```

### 任务 7：提供离线 CLI、合成端到端验收与中文合同

**文件：**

- 修改：`src/factor_miner/cli.py`
- 新建：`tests/test_llm_cli.py`
- 新建：`tests/test_llm_offline_e2e.py`
- 新建：`docs/contracts/v0.5-offline-protocol.md`
- 修改：`docs/superpowers/plans/2026-07-28-factor-miner-v0.3-v0.5-regime-coverage-and-llm.md`

**接口：**

- 消费：任务 1 至任务 6 的全部离线接口。
- 产出：`factor-miner llm` 命令组的 `brief-build-synthetic`、
  `family-register`、`event-append`、`family-seal`、`family-verify`；
  不产出在线生成或真实评价命令。

- [x] **步骤 1：编写 CLI 安全表面测试**

断言帮助中包含上述五个离线命令，不包含 `call-deepseek`、
`search-web`、`skip-privacy`、`skip-seal`、`force-strict`、
`read-outcome`、API Key 参数或任意 base URL 覆盖。

- [x] **步骤 2：运行测试并确认失败**

运行：

```bash
uv run python -m unittest tests.test_llm_cli tests.test_llm_offline_e2e -v
```

预期：失败，因为 `llm` 命令组尚不存在。

- [x] **步骤 3：接入只读输入和不可变输出 CLI**

所有命令读取 UTF-8 JSON、调用领域函数并输出规范 JSON。
`brief-build-synthetic` 要求显式 `--synthetic`，且拒绝
`FM_MODE=visible`；真实图谱 brief 构建留给后续公司 Linux 在线计划。
`family-seal` 只接受已经登记的 family 和完整事件链，不提供填槽、
改状态或跳过时间身份选项。

- [x] **步骤 4：编写完整纯合成端到端测试**

端到端流程：

```text
合成 coverage cards
→ 生成脱敏 brief
→ 登记四 arm / 120 槽 family
→ 追加 120 个明确终态事件
→ 投影并封存
→ 重放核验得到相同 family state 与 seal hash
```

第二条流程留下一个非终态槽，证明无法 seal；第三条流程篡改历史事件，
证明 verify 在任何投影前失败。全程 patch outcome opener，
断言没有读取真实数据或访问网络。

- [x] **步骤 5：编写中文运行合同并更新路线状态**

合同明确说明：

- 离线核心不连接外部 LLM，也不产生候选统计结论；
- strict temporal holdout 使用原始依赖区间，不使用生成时间；
- 五日标签、因子 lookback 和 discovery 派生统计全部进入信息边界；
- `reused_discovery_data` 只有探索性含义；
- Mac 只跑合成测试，公司 Linux 必须重跑完整离线套件；
- 下一阶段才实现字段 availability/DSL semantic type、外部调用录制、
  文献适配器、人工授权和真实 120 槽运行。

- [x] **步骤 6：运行离线 V0.5 与全量回归**

运行：

```bash
uv run python -m unittest \
  tests.test_llm_schema \
  tests.test_llm_brief \
  tests.test_llm_privacy \
  tests.test_llm_hypothesis \
  tests.test_llm_state \
  tests.test_llm_ledger \
  tests.test_llm_seal \
  tests.test_llm_cli \
  tests.test_llm_offline_e2e -v
uv run python -m unittest discover -s tests -v
```

预期：V0.5 离线测试与全部既有回归测试通过。

- [x] **步骤 7：提交**

```bash
git add src/factor_miner/cli.py tests/test_llm_cli.py tests/test_llm_offline_e2e.py docs/contracts/v0.5-offline-protocol.md docs/superpowers/plans/2026-07-28-factor-miner-v0.3-v0.5-regime-coverage-and-llm.md
git commit -m "feat: expose v0.5 offline protocol cli"
```

## 最终验收

- [x] 运行 `git status --short`，确认没有意外生成的真实数据、密钥、账本或产物进入工作树。
- [x] 运行 `rg -n "DeepSeek|DEEPSEEK_API_KEY|https?://" src/factor_miner/llm_*.py`，确认离线模块没有外部调用和密钥读取。
- [x] 运行 `rg -n "huan_quant|/Users/|/data/quantlake" src/factor_miner/llm_*.py tests/test_llm_*.py`，确认核心实现没有个人或公司路径依赖；隐私拒绝测试中的合成敏感字符串必须带清楚的测试注释。
- [x] 在公司 Linux 使用锁定提交和 `uv.lock` 重跑任务 7 的两组命令。
- [x] 记录当前完成范围为“V0.5 离线协议核心”，不得称为在线假设生成完成或真实候选评价完成。
