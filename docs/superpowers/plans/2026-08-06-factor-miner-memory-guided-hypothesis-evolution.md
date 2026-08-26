# 因子谱图与研究记忆驱动的十假设演化实现计划

## 文档信息

- 计划日期：2026-08-06
- 适用范围：阶段 C 之后的 V0.6 研究演化层
- 前置设计：[记忆辅助假设生成与因子实质差异约束设计](../specs/2026-08-06-factor-miner-memory-guided-hypothesis-evolution-design.md)
- 目标分支：`codex/v0.3-factor-coverage-map`
- 正式执行位置：公司 Linux 服务器；Mac 只运行脱敏的类型、哈希、状态机和纯合成测试

## 研究边界与完成定义

本阶段只增加“生成前利用历史记忆和现有因子谱图定位空白、人工批准十个假设、每个假设生成三种实质不同设计、完成 120 槽治理并把结果追加进记忆”的研究编排能力。它不把历史 IC、收益、组合 Sharpe 或任何原始行情交给外部模型，也不把筛选结果自动升级为有效 Alpha、证据或生产结论。

研究结论的边界保持不变：候选只有在公司 Linux 服务器通过冻结数据合同、可见验证、HAC/Bonferroni 多重检验、冗余检查和既定回测协议后，才能获得相应的候选层状态。经济机制仍然默认 `mechanism_unverified`；生成结果不能覆盖事前假设，失败、重复和未执行槽位都必须保留在分母和记忆中。

实现完成的判据如下：

- 每轮只允许一个不可变 `ResearchEvolutionContext`，固定一个已核验的 coverage graph、一个上一轮之前的 memory snapshot、一个 gap report、十个逻辑假设预算、每假设三个设计和四个 arm 的 120 槽预算。
- DeepSeek 只接收脱敏 gap brief、允许字段/算子族、可用性和设计差异合同；不得接收逐日 IC、收益、组合结果、原始行情、个股样本、完整数据库路径或密钥。
- 十个假设由同一批逻辑槽 `H01`–`H10` 表示；`coverage_outcome_llm` 与 `literature_only_llm` 复用这十个假设，不再分别生成十个假设。
- 每个 arm 内每个假设的 `C001`–`C003` 必须同时通过规范 AST 去重和“去掉时间参数后的结构签名”检查；只改变 `window` 或 `period` 的三件设计必须失败。失败槽仍然占用名额并进入 seal、统计分母和记忆。
- 人工批准数量不是十个时，流程必须在因子表达式生成和回测之前硬失败；系统不得自动补齐、自动批准或自动开启下一轮。
- 120 槽完成 seal、评价、发布后，每槽追加一条不可变研究记忆；下一轮只能读取本轮完成后构建的新 snapshot，不得在同一轮用结果反向改 prompt 或 gap 权重。
- PostgreSQL 仅作为 Dashboard/read model 投影；JSON/JSONL/Parquet 和内容哈希仍是正式主存储。旧污染 family `llmfamily_1bae19965638a6ac9620e0b0` 只能被拒绝或隔离，不能写入新 snapshot、gap brief、seal 或记忆。

## 实施顺序与提交边界

按以下顺序逐项实现。每项完成后先运行该项列出的测试，再单独提交该项代码和测试；不得把用户已有的阶段 A/B/C 脏改动混入这些提交。提交前用 `git diff -- <明确文件>` 检查范围，禁止使用覆盖工作树的 reset/checkout。

1. 不可变演化合同与哈希身份。
2. coverage graph 核验、gap report 和脱敏 brief。
3. 研究记忆追加存储与 snapshot。
4. 三设计实质差异闸门。
5. 十假设生成、人工批准和同批复用。
6. 120 槽编排、seal 绑定和失败状态。
7. 发布后记忆回写与 PostgreSQL/Dashboard 投影。
8. CLI、服务器运行手册、端到端合成验证和 Linux 复核。

每一项的测试都必须使用 `unittest` 和项目现有 fixture 风格；需要真实 coverage graph、A 股数据、IC、回测或 PostgreSQL 的测试在 Linux 隔离运行时执行，Mac 不得构造伪造的真实行情结论。

## 任务一：建立演化合同、记忆记录和设计策略模型

### 需要修改或新增的文件

- 新增 `src/factor_miner/research_evolution_schema.py`。
- 新增 `tests/test_research_evolution_schema.py`。
- 修改 `src/factor_miner/errors.py`，加入演化上下文、gap、批准数量、设计重复、记忆不可变性和污染 family 拒绝所需的明确 `FailureCode`。
- 只在需要导出公共模型时修改 `src/factor_miner/__init__.py`；不把 Skill、服务器路径或数据库连接写入核心包。

### 具体实现

在 `research_evolution_schema.py` 中使用冻结的 Pydantic 模型和 `extra="forbid"`，所有时间必须带时区，所有内容身份使用 `sha256_json`。定义以下接口，不允许调用方自行拼接身份字符串：

- `DesignDiversityPolicy`：固定 `hypotheses_per_round=10`、`designs_per_hypothesis=3`、`arms_per_round=4`、`total_slots=120`；定义允许的结构轴（字段集合、算子拓扑、时间角色、输入组合、节点数/深度范围）、禁止只改 temporal 参数、单个假设的相关性阈值和失败码版本。
- `CoverageGapCard`：保存 `gap_id`、gap 类别（`structural`、`market_regime`、`data_availability`）、脱敏标签、允许字段别名、允许算子族、时间窗口带、结构/信号簇数量带、缺失或失效风险和 `card_sha256`；不允许逐日数值、个股名、原始路径和未分桶指标。
- `CoverageGapReport`：绑定 `coverage_graph_id`、graph manifest hash、regime snapshot hash、memory snapshot hash、固定排序后的 gap cards、内部排序政策 hash 和 report hash。历史 IC/HAC/组合摘要只允许作为本地内部排序输入，报告的公开 brief 只保留预先定义的离散带和标签。
- `ResearchMemoryEntry`：至少包含 `memory_entry_id`、`entry_kind`、`discovery_family_id`、`generation_seal_id`、`run_id`、`hypothesis_slot_id`、`candidate_slot_id`、`hypothesis_spec_hash`、`candidate_spec_hash`、`ast_hash`、`coverage_graph_id`、`memory_snapshot_id`、字段/算子/时间/结构签名、gap 标签、终态、失败/重复原因、评价摘要、数据身份摘要、`supersedes_entry_id` 和 `entry_sha256`。评价摘要只允许冻结协议允许的汇总字段，不保存原始逐日序列。
- `ResearchMemorySnapshot`：保存 snapshot ID、创建时间、截断时间、来源 family/seal/run 列表、严格排序的 entry IDs 和 entry hashes、排除规则、entry count、coverage graph identity、snapshot hash。snapshot 必须是内容寻址且不可原地修改的。
- `ResearchEvolutionContext`：绑定新 family ID、coverage graph manifest hash、memory snapshot hash、gap report hash、field registry hash、evaluation policy hash、design policy hash、十假设/三设计/四 arm/120 槽预算、外部模型脱敏规则、上下文 hash。
- `EvolutionHypothesisDraft`、`EvolutionHypothesisApproval`、`ApprovedEvolutionHypothesisBatch`：逻辑槽只能是 `H01`–`H10`，每批必须正好十个且唯一；批准记录绑定 context hash、draft hash、family ID、审批角色、审批时间和决定 hash。批准模型保留 `mechanism_unverified`、事前主张、机制、独立验证、竞争解释、失效方式、证伪路径和 gap ID。

提供 `build_evolution_context(...)`、`build_gap_report_identity(...)`、`build_memory_entry_identity(...)` 和 `build_memory_snapshot_identity(...)` 四个纯函数，输入不完整或污染 family 时抛出明确的 `FactorMinerError`，不返回静默默认值。模型测试覆盖：内容寻址、时区、排序、正好十个槽、正好 120 预算、错误 hash、重复 H 槽、未知 gap 类别、污染 family 和超范围政策均失败。

### 验证与提交

~~~text
python -m unittest tests.test_research_evolution_schema
python -m unittest tests.test_llm_hypothesis tests.test_llm_state
~~~

检查错误信息能指出具体字段、槽位或 hash；通过后单独提交 `feat: add evolution research contracts`。

## 任务二：核验 coverage graph 并生成三类 gap report

### 需要修改或新增的文件

- 新增 `src/factor_miner/research_gap.py`。
- 新增 `tests/test_research_gap.py`。
- 复用并必要时补充 `src/factor_miner/coverage_snapshot.py`、`src/factor_miner/coverage_schema.py`、`src/factor_miner/coverage_workflow.py` 的 manifest/快照核验，不复制 graph schema。
- 若现有 `src/factor_miner/llm_brief.py` 有可复用的脱敏字段和序列化函数，抽取为内部公共函数并保留旧调用兼容；不要把 gap 计算塞进 LLM prompt builder。

### 具体实现

定义以下接口：

~~~python
def load_verified_coverage_graph(root: Path) -> VerifiedCoverageGraph: ...
def build_coverage_gap_report(
    graph: VerifiedCoverageGraph,
    memory_snapshot: ResearchMemorySnapshot,
    *,
    policy: GapSelectionPolicy,
) -> CoverageGapReport: ...
def build_sanitized_gap_brief(
    report: CoverageGapReport,
    *,
    field_registry_hash: str,
    design_policy: DesignDiversityPolicy,
) -> dict[str, object]: ...
~~~

`load_verified_coverage_graph` 必须检查 manifest、manifest hash、graph ID、factor nodes、structure edges、signal edges、performance、regime profile、cluster assignments 和 coverage summary 的存在性、哈希、版本及主键唯一性；缺一个就硬失败。它只读取显式传入的 graph root，不能从 Mac、`huan_quant`、个人目录或数据库兜底读取。

`build_coverage_gap_report` 固定三个方向：

- 结构空白：字段集合、算子拓扑、时间角色和输入组合的覆盖缺口。
- 市场状态空白：regime profile 中样本不足、稳定性不足或未覆盖的状态/状态组合。
- 数据可用性空白：字段发布日期、覆盖率、状态表和交易日可用性约束导致的可研究空间。

历史 IC、HAC、组合和冗余摘要只在本地转成离散优先级带，不能成为外部模型可读取的数值奖励，也不能在同一轮评价后重新计算。gap card 必须按 `gap_category`、风险等级、canonical gap hash 稳定排序，最多输出 10 张，记录其余被截断数量和原因。

`build_sanitized_gap_brief` 只输出 gap ID、标签、字段 alias、算子族、窗口带、结构轴要求、数据可用性标签、允许的失败风险标签和设计差异合同。输出递归扫描时若出现 IC、收益、Sharpe、最大回撤、逐日日期序列、ticker、服务器路径、密钥或任意未注册字段，立即失败。

测试使用临时的脱敏 synthetic graph fixture，覆盖 manifest/hash 错误、缺文件、主键重复、regime/profile 不一致、三类 gap 均有结果、确定性排序、历史数值不出 brief、空 gap 和污染 snapshot 拒绝。不要在 Mac fixture 中宣称 A 股研究结论。

### 验证与提交

~~~text
python -m unittest tests.test_research_gap tests.test_coverage_snapshot tests.test_coverage_schema
~~~

通过后单独提交 `feat: build sanitized coverage gap reports`。

## 任务三：实现正式研究记忆的追加写入和不可变 snapshot

### 需要修改或新增的文件

- 新增 `src/factor_miner/research_memory.py`。
- 新增 `tests/test_research_memory.py`。
- 复用 `src/factor_miner/ledger.py` 中已经验证过的单写入器、追加、原子写入、hash 链和损坏检测工具；若工具是私有函数，先抽取一个不改变旧行为的公共内部接口。
- 在 `src/factor_miner/runtime.py` 或现有配置入口中增加显式 `research_memory_root` 解析，禁止隐式写入项目根目录。

### 具体实现

正式布局固定为：

~~~text
<artifact_root>/research_memory/entries.jsonl
<artifact_root>/research_memory/entry_objects/<memory_entry_id>.json
<artifact_root>/research_memory/snapshots/<memory_snapshot_id>.json
<artifact_root>/research_memory/index.json
~~~

`entries.jsonl` 是追加账本；每行保存 event ID、entry hash、对象相对路径、写入时间和 `supersedes_entry_id`。对象文件和 snapshot 文件一旦存在只能逐字节复验，不能覆盖。index 只能通过追加事件更新，不是数据库失败时的 fallback。

实现以下接口：

~~~python
class ResearchMemoryStore:
    def append_entry(self, entry: ResearchMemoryEntry) -> Path: ...
    def load_entry(self, memory_entry_id: str) -> ResearchMemoryEntry: ...
    def build_snapshot(
        self,
        *,
        cutoff: datetime,
        source_family_ids: tuple[str, ...],
        coverage_graph_id: str,
        policy: MemorySnapshotPolicy,
    ) -> ResearchMemorySnapshot: ...
    def load_snapshot(self, memory_snapshot_id: str) -> ResearchMemorySnapshot: ...
    def verify(self) -> None: ...
~~~

写入时检查单写入器锁、JSONL 完整性、重复 event ID、同 ID 不同内容、对象 hash、entry family/seal/run 关系和污染 family。 `build_snapshot` 只接收 cutoff 之前已完成并已发布的 entry；按 `(created_at, memory_entry_id)` 排序，显式排除 `llmfamily_1bae19965638a6ac9620e0b0` 及任何不在当前研究配置白名单的 family。缺数据合同、发布清单、graph identity 或 entry hash 时失败，不删除失败记录、不压缩账本、不静默忽略损坏行。

快照完成后，下一轮 context 只保存 snapshot hash；同轮生成、表达式验证和评价过程不得读取本轮新追加的 entry。对同一槽位的后续纠正使用 `supersedes_entry_id` 追加新记录，旧记录仍在分母中。

测试覆盖：追加后不可覆盖、重复 ID 同内容可幂等确认、重复 ID 异内容失败、JSONL 截断失败、snapshot hash 复验、cutoff 边界、污染 family 排除、DB 不存在时仍能从正式文件恢复、缺发布 manifest/数据身份失败和并发写入器失败。

### 验证与提交

~~~text
python -m unittest tests.test_research_memory tests.test_ledger tests.test_llm_ledger
~~~

通过后单独提交 `feat: add immutable research memory store`。

## 任务四：实现三因子设计实质差异闸门

### 需要修改或新增的文件

- 新增 `src/factor_miner/design_diversity.py`。
- 新增 `tests/test_design_diversity.py`。
- 修改 `src/factor_miner/dsl.py`（只抽取可复用的 AST 遍历/时间参数规范化函数，不放宽白名单）。
- 修改 `src/factor_miner/llm_candidate.py`，让表达式生成结果在进入候选槽终态前必须经过该闸门。

### 具体实现

定义：

~~~python
@dataclass(frozen=True, slots=True)
class DesignSignature:
    canonical_ast_hash: str
    ast_without_temporal_parameters_hash: str
    field_set: tuple[str, ...]
    operator_topology: tuple[str, ...]
    temporal_roles: tuple[str, ...]
    input_combinations: tuple[tuple[str, ...], ...]
    node_count: int
    depth: int

def preflight_designs(
    expressions: tuple[FactorExpression, FactorExpression, FactorExpression],
    *,
    policy: DesignDiversityPolicy,
) -> DesignDiversityResult: ...
~~~

先用现有 typed AST parser、`dsl_semantics` 和 deterministic compiler 做类型、单位、未来信息、节点数、深度、lookback 和字段可用性校验；无效 AST 不能先被转换成签名后放行。然后计算完整 canonical AST hash，再递归删除 `period`/`window` 等时间参数计算 `ast_without_temporal_parameters_hash`。三件设计若完整 hash 重复，或去掉时间参数后的结构 hash 重复，必须返回 `design_diversity_failed`；仅改变时间窗口、延迟期、差分期或滚动期不得作为不同设计。通过条件至少包含一个非时间结构轴变化：字段集合、算子拓扑、时间角色、输入组合、节点数或深度中的一项发生变化，并且三件设计的设计签名按槽顺序可复验。

每个失败结果保存具体 pair、轴名称、两个 hash、失败码和原始槽 ID。不能用“模型声称不同”代替 AST 检查，也不能因失败而减少 trial denominator。跨 arm 的 exact AST 重复不删除任何槽；另行写入 `cross_arm_redundant` 记忆标签供下一轮 gap 排序。

测试使用真实 DSL 结构的 synthetic AST，至少覆盖：完全相同、只换 `rolling_mean.window`、只换 `delta.period`、更换字段、改变算子拓扑、增加输入组合、非法 centered rolling、未知字段和超 lookback。测试断言失败槽可序列化、顺序确定、错误不会被吞掉。

### 验证与提交

~~~text
python -m unittest tests.test_design_diversity tests.test_dsl tests.test_dsl_semantics tests.test_llm_candidate
~~~

通过后单独提交 `feat: enforce structural factor design diversity`。

## 任务五：实现十假设生成、人工批准和两 LLM arm 同批复用

### 需要修改或新增的文件

- 新增 `src/factor_miner/research_evolution.py`，负责 context、gap brief、memory snapshot、LLM 请求和批准批次之间的编排。
- 修改 `src/factor_miner/llm_agents.py`，加入逻辑槽 `H01`–`H10` 的演化请求和严格响应解析。
- 修改 `src/factor_miner/llm_online.py`，让 DeepSeek 请求授权绑定 context hash、gap report hash、model scope 和脱敏 brief hash。
- 修改 `src/factor_miner/llm_hypothesis.py`，保留旧的 `CoverageGapHypothesisDraft` 兼容层，并增加从逻辑 `EvolutionHypothesisDraft` 到 arm-specific draft 的显式适配器。
- 修改 `src/factor_miner/llm_literature.py`，让文献检索只接收逻辑假设和 source record 任务，不重新生成第二套假设。
- 新增 `tests/test_research_evolution.py`，并扩展 `tests/test_llm_agents.py`、`tests/test_llm_online.py`、`tests/test_llm_hypothesis.py`、`tests/test_llm_literature.py`。

### 具体实现

逻辑假设不再把 arm 写进身份。定义：

~~~python
def build_evolution_hypothesis_request(
    context: ResearchEvolutionContext,
    gap_brief: dict[str, object],
    *,
    authorization: LLMRequestAuthorization,
) -> PreparedDeepSeekRequest: ...

def parse_evolution_hypothesis_response(
    response: Mapping[str, object],
    *,
    context: ResearchEvolutionContext,
) -> tuple[EvolutionHypothesisDraft, ...]: ...

def approve_evolution_hypotheses(
    drafts: tuple[EvolutionHypothesisDraft, ...],
    decisions: tuple[EvolutionHypothesisApproval, ...],
    *,
    context: ResearchEvolutionContext,
) -> ApprovedEvolutionHypothesisBatch: ...
~~~

请求 builder 只序列化 gap brief、允许的字段 alias、算子族、数据可用性标签、时间窗口带、结构轴和“三设计必须不同”的硬合同；禁止把 gap report 的内部历史数值列入 prompt。响应解析必须正好返回十个逻辑槽，编号完整覆盖 `H01`–`H10`，不接受重复 gap/重复内容来补数量，不接受额外槽，不接受模型修改 evaluation policy、alpha、HAC、multiplicity family 或数据合同。每个草案保留事前主张、机制、预期方向、可观察代理、独立验证、竞争解释、失效模式、证伪路径、来源记录和目标 gap。

人工批准接口先核验 draft hash、context hash、family ID、审批角色和时间，再要求十个槽各有一条 `approved` 决定。任何一个 `rejected`、缺失、重复、未核验来源或不足十个，都返回 `approval_required`/`hypothesis_budget_invalid`，并阻止表达式请求和回测。批准结果内容寻址，不能原地编辑。

现有 `CoverageGapHypothesisDraft.slot_id` 仍按旧接口要求使用 `arm_id:Hxx`，但只能由适配器从同一个逻辑 `ApprovedEvolutionHypothesisBatch` 生成，不能让两个 arm 重新各自审批。 `coverage_outcome_llm` 和 `literature_only_llm` 的请求都携带同一批十个逻辑 hypothesis IDs、同一个 approval hash 和同一个 context hash；差异只来自已冻结的生成路线和文献任务。

测试覆盖：正好十个、九个/十一个、重复 H 槽、重复内容、模型试图覆盖政策、prompt 不含历史数值和原始数据、请求 hash 稳定、context/gap/memory hash 绑定、批准前阻断、两 arm 共用同一逻辑 batch、污染 family 被拒绝。使用假的 provider 响应，不调用网络。

### 验证与提交

~~~text
python -m unittest tests.test_research_evolution tests.test_llm_agents tests.test_llm_online tests.test_llm_hypothesis tests.test_llm_literature
~~~

通过后单独提交 `feat: gate ten approved evolution hypotheses`。

## 任务六：把设计闸门接入 120 槽生成、shadow arm 和 generation seal

### 需要修改或新增的文件

- 修改 `src/factor_miner/llm_orchestrator.py`：用一个批准的十假设 batch 生成两 LLM arm 和两 deterministic shadow arm 的槽映射。
- 修改 `src/factor_miner/research_campaign_runner.py`：在表达式生成后、候选 READY 前运行三设计 preflight，并将失败槽终结化。
- 修改 `src/factor_miner/llm_state.py`：加入 `DESIGN_DIVERSITY_FAILED` 等终态并更新合法迁移、终态集合和 `expected_candidate_slot_ids` 对应校验。
- 修改 `src/factor_miner/llm_seal.py`：在 `GenerationSealManifest` 增加 `evolution_context_hash`、`coverage_graph_manifest_hash`、`memory_snapshot_hash`、`gap_report_hash`、`approval_batch_hash`、`design_policy_hash` 和 `logical_hypothesis_ids_hash`，并在 build/verify 中强制复验。
- 修改 `src/factor_miner/shadow_generators.py`：shadow arm 只复用已登记 coverage parent 和相同十逻辑假设，不自行产生新的假设；每组三设计仍通过相同的 preflight。
- 扩展 `tests/test_research_campaign_120_slots.py`、`tests/test_research_campaign_runner.py`、`tests/test_llm_orchestrator.py`、`tests/test_llm_seal.py`，新增 `tests/test_campaign_design_diversity.py`。

### 具体实现

把 `run_approved_campaign_generation` 和 `MappingCampaignPilotRunner` 增加必需的 `ApprovedEvolutionHypothesisBatch`/`ResearchEvolutionContext` 参数（旧的非演化 Stage C 调用保留兼容，但新 CLI 的 `--evolution-context` 路径必须走严格合同）。新路径执行顺序固定为：

1. 核验批准 batch 正好十个、context/family/graph/memory/gap hash 一致。
2. 为每个 `Hxx` 在每个 arm 创建 `C001`、`C002`、`C003`，先登记 120 个槽位，再读取表达式结果。
3. 对每个 arm 的三件表达式运行 typed AST、semantic、compiler 和 `preflight_designs`；只换窗口/period 的结果进入 `DESIGN_DIVERSITY_FAILED`，不得进入 IC、回测或 ready dependency。
4. 对可计算候选运行既有可见验证、IC、冗余和回测协议；失败也保留 slot object hash 和 failure reason。
5. 在 `finalize_unexecuted_slots` 后确认所有 120 槽均为终态，再构建 seal。

失败槽的 object 必须包含逻辑 hypothesis ID、arm ID、candidate slot ID、context hash、design signatures（若可得）、failure code、失败阶段和 created_at；不得只写一条无上下文字符串。 `DESIGN_DIVERSITY_FAILED` 不得有 evaluation dependency interval。seal 必须拒绝缺少任何新身份字段、非 120 槽、缺设计失败对象或引用错误 graph/memory/gap 的状态。

测试断言：十假设 × 三设计 × 四 arm 产生完整 120 槽；每 arm 的 window-only 三件套均在评价前失败；结构不同的三件套可以继续；一个 arm 失败不减少其他 arm 的槽；shadow arm 不新增 hypothesis；同批两个 LLM arm 的 approval/context hash 相同；seal 能发现篡改任一 evolution hash；120 槽终态和统计分母保持不变。

### 验证与提交

~~~text
python -m unittest tests.test_campaign_design_diversity tests.test_research_campaign_120_slots tests.test_research_campaign_runner tests.test_llm_orchestrator tests.test_llm_seal tests.test_llm_state tests.test_shadow_generators
~~~

通过后单独提交 `feat: bind evolution context to 120-slot seal`。

## 任务七：发布后追加记忆并投影到 PostgreSQL/Dashboard

### 需要修改或新增的文件

- 修改 `src/factor_miner/research_campaign_runner.py`：只在 immutable publication 和 generation seal 验证成功后调用记忆追加。
- 修改 `src/factor_miner/research_memory.py`：增加从 120 槽终态、候选 Spec、评价摘要和发布 manifest 构建 entry 的函数。
- 新增 `dashboard/migrations/006_research_evolution_memory.sql`。
- 修改 `dashboard/pg_store.py`：增加幂等投影和 hash 冲突硬失败。
- 修改 `src/factor_miner/dashboard_projection.py`、`dashboard/project.py`：把 evolution context、gap 摘要、approval 数量、120 槽终态计数和 memory snapshot 引用加入只读快照；旧运行没有这些字段时保持兼容。
- 修改或新增 Dashboard 页面（现有 `dashboard/pages/1_批次总览.py` 及同目录下的页面），只展示脱敏标签、状态和可审计 identity，不展示原始行情或外部模型原文。
- 新增 `tests/test_research_memory_projection.py`，扩展 `tests/test_dashboard_projection.py`、`tests/test_dashboard_contract.py`。

### 数据库读模型

新增迁移创建以下只读投影表，并使用稳定 primary key/hash 唯一约束：

- `research_evolution_batches`：`context_id`、`context_sha256`、`discovery_family_id`、`approval_batch_hash`、`coverage_graph_id`、`coverage_graph_manifest_hash`、`memory_snapshot_hash`、`gap_report_hash`、`design_policy_hash`、`hypothesis_count`、`slot_count`、`created_at`、`source_manifest_sha256`。
- `research_gap_summaries`：`gap_report_hash`、`context_id`、三类 gap 数量、脱敏标签 JSONB、graph/memory identity 和 created_at；不保存历史逐日指标和原始数据。
- `research_memory_snapshots`：`memory_snapshot_id`、`snapshot_sha256`、`coverage_graph_id`、`entry_count`、`source_family_ids`、`cutoff_at`、`created_at`。
- `research_memory_entries`：`memory_entry_id`、family/seal/run/hypothesis/candidate IDs、spec/AST/graph/snapshot hashes、status、failure/redundancy code、结构标签、有限评价摘要、entry hash、created_at 和 source manifest hash。

稳定审计列使用明确类型；只对受控的脱敏 tags 和有限 summary 使用 JSONB。禁止 Dashboard 直接 `UPDATE`/`DELETE` 正式记忆；同一主键如果 hash 不同必须回滚并报错，同一主键同 hash 可幂等重放。PostgreSQL 断开时发布和记忆正式写入仍必须成功或整体失败，不得切换到数据库作为主存储。

`publish_campaign_evaluation` 的顺序固定为：验证 seal → 写不可变发布 manifest → 追加 120 条最终 memory entries → 写 snapshot/reference → 更新 DB read model。任一步失败都不能标记发布完成；若 DB 投影失败，保留正式产物和明确的待投影状态，不能重算或篡改 entry。

测试使用 fake store 验证相同 snapshot 可重复投影、不同 hash 冲突失败、120 条包括失败/重复/未执行槽、旧运行无 evolution metadata 时兼容、污染 family 不被投影、原始数据和模型原文不出现在数据库 payload。迁移测试检查表、索引、约束和最小只读权限，禁止出现 `Evidence` 或 `Conclusion` 产物目录名。

### 验证与提交

~~~text
python -m unittest tests.test_research_memory_projection tests.test_dashboard_projection tests.test_dashboard_contract tests.test_research_campaign_evaluation
~~~

通过后单独提交 `feat: project evolution memory to dashboard`。

## 任务八：增加 CLI 人工闸门、运行手册和可审计入口

### 需要修改或新增的文件

- 修改 `src/factor_miner/cli.py`。
- 新增 `docs/runbooks/记忆辅助假设生成与120槽运行.md`。
- 修改已有 `docs/contracts/v1自动化研究与可视化运行合同.md`，追加 V0.6 演化合同，不删除阶段 A/B/C 约束。
- 如需展示状态，修改 `dashboard/pages/1_批次总览.py`；页面文案和新增 Markdown 全部使用中文。
- 扩展 `tests/test_cli.py`、`tests/test_llm_cli.py`、`tests/test_llm_run_approved_cli.py`，新增 `tests/test_research_evolution_cli.py`。

### CLI 合同

增加以下正式命令，参数必须显式给出 artifact root、coverage graph root、memory root、policy/config 和输出路径：

~~~text
factor-miner evolution prepare-context \
  --coverage-graph-root <verified-root> \
  --memory-root <formal-root> \
  --family-id <new-family> \
  --policy <frozen-policy.json> \
  --output <context.json>

factor-miner evolution generate-hypotheses \
  --context <context.json> \
  --authorization <request-authorization.json> \
  --output <draft-batch.json>

factor-miner evolution approve-hypotheses \
  --context <context.json> \
  --draft-batch <draft-batch.json> \
  --decisions <human-decisions.json> \
  --output <approved-batch.json>

factor-miner campaign run-approved \
  --evolution-context <context.json> \
  --approved-hypotheses <approved-batch.json> \
  ...existing frozen Stage-C arguments...
~~~

`prepare-context` 只使用 cutoff 前的已完成记忆和指定 graph；`generate-hypotheses` 可以调用已授权的 DeepSeek，但保留 request hash、response digest、provider/model identity 和脱敏 prompt bundle，不保存原始模型响应；`approve-hypotheses` 是唯一把草案变成可执行批准 batch 的入口；`campaign run-approved` 在数量不是十、approval/context/family/graph/memory hash 不一致或旧污染 family 出现时退出非零，且在创建任何 factor expression/backtest 任务前失败。

运行手册必须记录：研究人员先查看现有谱图的三类 gap 摘要，再让模型生成十个假设，再人工逐条批准，随后才生成表达式和回测；如何检查每个 Hxx 的三个设计签名；如何解释 `DESIGN_DIVERSITY_FAILED`、`cross_arm_redundant`、`not_executed`；如何验证 seal、memory snapshot 和 Dashboard projection；如何在失败时保留账本并重新提交纠正事件。手册不得写入 SSH 密码、公司原始路径解析结果、真实个股样本或模型密钥。

### 验证与提交

~~~text
python -m unittest tests.test_research_evolution_cli tests.test_cli tests.test_llm_cli tests.test_llm_run_approved_cli
~~~

通过后单独提交 `docs: document memory-guided evolution operations`。

## 任务九：合成端到端验证和公司 Linux 复核

### Mac 纯合成验证

新增或扩展 `tests/test_research_evolution_synthetic_e2e.py`，用脱敏 graph、假的十假设 response、人工批准 payload、三个结构不同或 window-only 的 DSL 表达式、四个 arm 的 shadow fixture 和 fake Dashboard store 验证以下流程：

~~~text
coverage graph manifest
    -> memory snapshot
    -> structural/regime/data gap report
    -> sanitized DeepSeek request
    -> exactly ten drafts
    -> human approval gate
    -> 120 registered slots
    -> design diversity terminal failures
    -> seal
    -> synthetic evaluation publication
    -> 120 memory entries
    -> next snapshot excludes current round
    -> dashboard read model
~~~

测试必须断言人工只批准九个时没有 expression request、没有 backtest、没有 publication；window-only 设计不会进入 ready；所有 120 槽都在 seal 和 memory 中可见；下一轮 context 的 snapshot 不包含同轮 entry；旧污染 family hash 未被读取或写入；请求和 DB payload 不含原始数据/外部模型原文。

### 公司 Linux 复核

实现完成后才通过 SSH 进入公司 Linux 隔离运行时，先执行完整 `unittest` 和 CLI help，再使用新的合法 family、显式 graph root、显式 memory root 和一批人工批准的十假设执行 dry-run/合成编排。只有用户另行批准并确认数据合同、截止日、评价政策和回测预算后，才运行真实 A 股候选计算、IC、冗余和回测。正式运行前后分别保存非敏感审计摘要：代码提交、配置 hash、graph manifest hash、memory snapshot hash、approval hash、seal hash、slot 状态计数、DB projection hash。

复核时明确检查：

- `/data/quantlake` 仍只读，所有产物写入显式独立根目录。
- 真实候选、IC、回测和统计只在 Linux 运行；Mac 不参与任何真实结论。
- 旧污染 family 的 spec hash、ledger tip 和文件 mtime/hash 未发生变化。
- 任何缺字段、状态表落后、发布/复权口径未知、主键重复、截止日不一致、账本损坏或 graph/memory hash 不一致均硬失败。
- PostgreSQL 只是投影，Dashboard 显示的 context/gap/memory identity 与正式产物一致。

### 验证与提交

先运行：

~~~text
python -m unittest discover -s tests -p 'test_*.py'
~~~

然后在服务器执行项目合同规定的 Linux 验证命令，并把非敏感摘要追加到运行手册或受控运行记录中；不得把真实结果、账本、模型、密钥、数据库 dump 或服务器路径解析结果提交 Git。若只完成代码而没有新的十假设批准和真实运行，不得把任务描述为“已完成生产因子生成”。

## 交付后的人工操作顺序

实现和 Linux 验证完成后，实际每轮必须遵守以下顺序：

1. 研究员确认 coverage graph、regime snapshot、字段可用性和上一轮 memory snapshot。
2. 系统生成并展示三类 gap 的脱敏摘要及其选择理由。
3. 系统只生成十个逻辑假设草案，保存 draft hash。
4. 研究员逐条批准或拒绝；不是十个 approved 时流程停止。
5. 系统按同一批准批次生成每假设三个候选设计，先做实质差异闸门。
6. 研究员确认设计失败/重复计数后，才开始服务器上的 IC、冗余和回测评价。
7. 系统封存 120 槽、发布结果、追加全部槽位记忆并投影 Dashboard。
8. 下一轮只能从新 snapshot 开始；系统不得自动批准或自动连续生成。

完成本计划不会改变“候选不等于有效 Alpha”的结论边界，也不会触碰已隔离的旧污染 family。
