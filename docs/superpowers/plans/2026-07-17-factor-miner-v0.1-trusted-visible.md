# Factor Miner V0.1 可信可见验证实施计划

> **代理执行要求：** 实施时必须使用 `superpowers:executing-plans`，逐项完成本计划。步骤使用复选框（`- [ ]`）跟踪。除非用户明确要求，否则本仓库禁止使用子代理执行。

**目标：** 在不增加大语言模型或认证功能的前提下，使可见验证路径具备封闭失败、结果隔离、时点正确、因子冗余识别和完整审计能力。

**架构：** 用三个职责单一的数据端口和版本化、由代码解析的评价政策，替代混合数据源与批次自行持有的评价设置。每次运行依次经过结果前校验、原始因子计算、结果评价、冗余检查和暂存产物发布，最后才写入账本终态事件。

**技术栈：** Python 3.12、Pydantic 2、Polars 1、statsmodels 0.14、Typer、unittest、规范 JSON/JSONL 和 Parquet。

## 全局约束

- 所有真实因子计算、IC、统计推断和冗余检查只能在公司 Linux 服务器运行。
- Mac 测试只使用程序生成的合成数据。
- V0.1 只产出通过可信可见验证的候选因子，不得称为 Evidence、认证 Alpha 或生产结论。
- V0.1 使用 HAC 与 Bonferroni 评价逐日 RankIC；没有策略收益时不得计算 CPCV 或 Deflated Sharpe Ratio，也不得用它们替代因子 IC 的多重检验。
- 核心包不得导入 `huan_quant`、个人 Skill、个人路径、数据库或任意因子 Python 代码。
- 候选、研究族、政策和批次对象不可变并采用内容寻址。
- 编译、结构冗余和动态未来依赖检查通过前，不得打开结果数据。
- 每个尝试过的假设都消耗一个不可复用的全局研究族名额。
- 合同、参考因子、来源、状态、产物或终态缺失时必须失败，不提供兜底。

---

### 任务 1：冻结可信评价政策、交易时点、股票池和全局研究预算

**文件：**

- 修改：`src/factor_miner/schema.py`
- 修改：`src/factor_miner/ledger.py`
- 新建：`src/factor_miner/policy.py`
- 修改：`tests/helpers.py`
- 修改：`tests/test_schema.py`
- 修改：`tests/test_ledger.py`
- 修改：`examples/candidates/momentum_20d.json`
- 修改：`examples/campaigns/synthetic_visible.json`

**接口：**

- 产出：`AvailabilitySpec`、`TrustedCandidateFactorSpec`、`RegisteredTrustedCandidate`、`UniverseSpec`、`EvaluationPolicySpec`、`ResearchFamilySpec`、`RegisteredResearchFamily`、`TrustedVisibleCampaignSpec`、`company_a_share_visible_policy()`、`evaluation_policy_id()`、`registered_research_family()`，以及可信批次对政策和研究族 ID 的校验。
- 依赖：现有规范 JSON、内容哈希和不可变文档写入器。

- [x] **步骤 1：编写预期失败的结构测试**

增加测试，拒绝 `TrustedCandidateFactorSpec` 中的自由文本可得性、`TrustedVisibleCampaignSpec` 中的旧版候选 ID、可信批次上的任意标签/掩码/显著性字段、非时点正确股票池、小于已分配名额的研究族预算、重复名额编号、政策与研究族 ID 不匹配，以及未被冻结研究族分配覆盖的可信批次候选。断言内置政策固定使用 `label_o2o_5d`、`valid_for_factor_rank`、`alpha=0.05`、`neutralization="none"` 和 `earliest_trade="open_t_plus_1"`。

- [x] **步骤 2：运行测试并确认失败**

运行：`uv run python -m unittest tests.test_schema tests.test_ledger -v`

预期：失败，因为 V0.1 政策、研究族类型和账本文档尚不存在。

- [x] **步骤 3：实现不可变的 V0.1 对象**

使用冻结的 Pydantic 模型，并设置 `extra="forbid"`。新增固定版本 `"2"` 的 `TrustedCandidateFactorSpec`，以及 `AvailabilitySpec(observation="close_t", decision="after_close_t", earliest_trade="open_t_plus_1")`。新增 `TrustedVisibleCampaignSpec`，包含可见验证日期、有序可信候选 ID、`evaluation_policy_id` 和 `research_family_id`。V0 的 `CandidateFactorSpec` 和 `CampaignSpec` 仅用于不可变历史与现有验收测试。由代码解析的公司政策包含全部评价和冗余阈值。`ResearchFamilySpec` 将每个可信候选 ID 映射到从一开始的唯一名额，并冻结 `global_hypothesis_budget`。

- [x] **步骤 4：持久化政策和研究族文档**

为 `LedgerPaths` 增加 `policies_root` 和 `families_root`；使用现有规范原子写入器实现幂等、不可变登记。登记必须拒绝内容与地址不匹配，并且绝不修改已有文档。

- [x] **步骤 5：迁移合成示例和测试**

增加可信测试夹具，用于构造 V0.1 候选、内置政策、单名额研究族和引用这些 ID 的可信批次。保留旧夹具以覆盖 V0 回归测试。旧 V0 账本字节保持不变；不增加静默迁移加载器。

- [x] **步骤 6：验证并提交**

运行：`uv run python -m unittest tests.test_schema tests.test_ledger -v`

预期：通过。

提交：`feat: freeze trusted visible research policy`

### 任务 2：物理分离因子输入、研究结果和参考因子

**文件：**

- 修改：`src/factor_miner/data_source.py`
- 修改：`src/factor_miner/company_a_share.py`
- 修改：`src/factor_miner/compute.py`
- 修改：`src/factor_miner/evaluation.py`
- 修改：`tests/test_data_source.py`
- 修改：`tests/test_compute.py`
- 修改：`tests/test_evaluation.py`

**接口：**

- 产出：`FactorInputRequest`、`OutcomeRequest`、`InputProvenance`、`OutcomeProvenance`、`ReferenceProvenance`、`FactorInputSource`、`OutcomeSource` 和 `ReferenceFactorSource`。
- 依赖：任务 1 的政策和股票池类型。

- [x] **步骤 1：编写端口隔离测试**

创建带独立计数器的侦察数据源。断言输入检查只读取行情和状态结构，输入扫描绝不包含标签列，结果检查会校验政策中的标签合同，参考因子读取要求清单 ID 和哈希一致。

- [x] **步骤 2：运行测试并确认失败**

运行：`uv run python -m unittest tests.test_data_source tests.test_compute tests.test_evaluation -v`

预期：失败，因为混合 `DataSource` 仍然无条件连接标签。

- [x] **步骤 3：实现窄端口和公司数据适配器**

将当前适配器拆分为 `CompanyAShareFactorInputSource`、`CompanyAShareOutcomeSource` 和 `ParquetReferenceFactorSource`。只共享纯合同辅助函数；不得让输入检查接受标签 URI。`compute_raw_factor` 只依赖 `FactorInputSource`；评价阶段在工作流内部将原始因子与 `OutcomeSource` 数据连接。

- [x] **步骤 4：验证评价前结果数据访问次数为零**

运行：`uv run python -m unittest tests.test_data_source tests.test_compute tests.test_evaluation -v`

预期：通过，包括用计数器断言证明纯输入路径没有访问标签。

- [x] **步骤 5：提交**

提交：`refactor: isolate factor inputs from outcomes`

### 任务 3：强制可见验证路径并核验真实来源

**文件：**

- 修改：`src/factor_miner/runtime.py`
- 修改：`src/factor_miner/cli.py`
- 修改：`configs/company_a_share.env.example`
- 修改：`tests/test_runtime.py`
- 修改：`tests/test_cli.py`
- 修改：`docs/contracts/company-a-share-data.md`

**接口：**

- 产出：已核验的发布、配置、代码和锁文件身份，以及按运行模式隔离的输入根目录。
- 依赖：任务 2 拆分后的适配器。

- [x] **步骤 1：编写预期失败的边界测试**

测试可见验证会拒绝位于已配置 QuantLake 发布目录之外的行情或状态数据，拒绝 `/data/factor_miner_artifacts/inputs` 下的所有 URI，拒绝非十六进制或不匹配的清单 SHA-256，拒绝与实际内容不符的 Git HEAD 或配置哈希，并拒绝脏工作区。测试冒烟测试只能使用有界输入根目录，且不能创建结果数据源。

- [x] **步骤 2：运行测试并确认失败**

运行：`uv run python -m unittest tests.test_runtime tests.test_cli -v`

预期：失败，因为 V0 路径过于宽松且只检查哈希字符串。

- [x] **步骤 3：实现内容核验**

在私有配置中增加明确的清单和配置路径；对文件字节计算 SHA-256；将发布 ID 和数据集根目录与清单内容比较；将 `FM_CODE_COMMIT` 与 `git rev-parse HEAD` 比较；要求 `git status --porcelain` 为空；保留现有 `uv.lock` 哈希。所有失败统一使用 `RUNTIME_BOUNDARY_ERROR`。

- [x] **步骤 4：更新诊断命令和数据合同**

诊断命令分别报告 `inputs_checked`、`outcomes_checked` 和 `references_checked` 布尔值及解析后的哈希。它只校验合同，不计算因子，也不读取标签值。

- [x] **步骤 5：验证并提交**

运行：`uv run python -m unittest tests.test_runtime tests.test_cli -v`

预期：通过。

提交：`feat: verify visible data provenance`

### 任务 4：按交易观察数预热并增加动态未来依赖探针

**文件：**

- 修改：`src/factor_miner/data_source.py`
- 修改：`src/factor_miner/company_a_share.py`
- 新建：`src/factor_miner/lookahead.py`
- 修改：`src/factor_miner/workflow.py`
- 修改：`tests/test_data_source.py`
- 新建：`tests/test_lookahead.py`
- 修改：`tests/test_workflow.py`

**接口：**

- 产出：`LookaheadProbeResult` 和 `run_lookahead_probes(plan, input_frame, checkpoints, seed)`。
- 依赖：`FactorInputRequest.warmup_observations`、编译计划和纯输入数据。

- [x] **步骤 1：编写预期失败的预热与泄漏测试**

创建合成工作日历，使 120 个交易观察需要超过 120 个自然日。断言系统会载入足够历史，并在单个资产历史不足时失败。增加一个故意依赖未来的测试表达式适配器，证明前缀截断与未来扰动可以识别它，同时所有白名单计划都能通过。

- [x] **步骤 2：运行测试并确认失败**

运行：`uv run python -m unittest tests.test_data_source tests.test_lookahead tests.test_workflow -v`

预期：失败，因为预热仍按自然日计算，且动态探针尚不存在。

- [x] **步骤 3：实现按观察数预热**

从版本化交易日历解析读取下界，并在计算前校验每个资产至少拥有 `warmup_observations` 行开始日前数据。重命名全部 `warmup_days` 字段，并从生产代码删除 `timedelta(days=lookback)`。

- [x] **步骤 4：在打开结果前实现确定性探针**

在冻结的检查点上，比较完整输入、前缀输入和未来扰动输入在最后一个未受影响日期之前的输出。保留空值相等语义，对确定性 Polars 操作使用精确相等。记录检查点、随机种子、比较行数、输入哈希和通过/失败结果。在不打开结果数据的情况下抛出 `LOOKAHEAD_DETECTED`。

- [x] **步骤 5：验证并提交**

运行：`uv run python -m unittest tests.test_data_source tests.test_lookahead tests.test_workflow -v`

预期：通过。

提交：`feat: validate point-in-time factor computation`

### 任务 5：强制执行结构冗余和输出冗余检查

**文件：**

- 修改：`src/factor_miner/redundancy.py`
- 修改：`src/factor_miner/workflow.py`
- 修改：`src/factor_miner/cli.py`
- 修改：`tests/test_redundancy.py`
- 修改：`tests/test_workflow.py`
- 修改：`tests/test_cli.py`

**接口：**

- 产出：对内置参考因子和批次内先前候选执行封闭失败的冗余检查。
- 依赖：`ReferenceFactorSource`、政策参考清单、编译计划和原始因子数据。

- [x] **步骤 1：编写预期失败的工作流和 CLI 测试**

断言完全相同的 AST 在输入检查前失败；已声明但缺失参考元数据或数据时失败；与参考因子高度正相关或负相关时失败；批次内更早的候选会自动纳入比较；任何候选都不能在 `redundancy=None` 或参考集合不完整时通过。

- [x] **步骤 2：运行测试并确认失败**

运行：`uv run python -m unittest tests.test_redundancy tests.test_workflow tests.test_cli -v`

预期：失败，因为 V0 只有在可选映射为真时才检查输出冗余，而且从未调用结构冗余检查。

- [x] **步骤 3：接入两道冗余闸门**

按冻结顺序编译批次候选，在读取输入前执行结构比较，以封闭失败方式加载政策中的每个参考因子，并将原始输出与参考因子及所有更早候选的输出比较。为每个候选持久化完整比较结果，包括失败和重叠不足情况。

- [x] **步骤 4：验证并提交**

运行：`uv run python -m unittest tests.test_redundancy tests.test_workflow tests.test_cli -v`

预期：通过。

提交：`fix: enforce visible redundancy gates`

### 任务 6：先发布完整产物，再写终态事件，并恢复中断运行

**文件：**

- 修改：`src/factor_miner/workflow.py`
- 修改：`src/factor_miner/ledger.py`
- 修改：`src/factor_miner/cli.py`
- 修改：`tests/test_workflow.py`
- 修改：`tests/test_ledger.py`
- 修改：`tests/test_cli.py`

**接口：**

- 产出：运行暂存发布器、完整清单、`recover-interrupted` 行为和可审计候选包。
- 依赖：任务 1 至任务 5 的全部结果。

- [x] **步骤 1：编写预期失败的产物顺序测试**

断言每个候选都具有执行计划、未来依赖探针、原始因子、质量、评价、推断、冗余和候选包文件，且清单哈希一致。在重命名前注入失败，证明不存在通过事件。留下暂存目录和 `run_started` 事件，证明下次恢复会追加 `interrupted`，同时不删除任何字节。

- [x] **步骤 2：运行测试并确认失败**

运行：`uv run python -m unittest tests.test_workflow tests.test_ledger tests.test_cli -v`

预期：失败，因为 V0 在最终产物包完整之前就写入终态事件。

- [x] **步骤 3：实现暂存发布**

在 `artifacts/.staging/<run_id>` 下写入规范 JSON 和 Parquet，对每个文件和目录执行 `fsync`，根据实际哈希生成清单，然后原子重命名到 `artifacts/runs/<run_id>`。只有最终发布完成后才追加候选终态事件。候选包保留“仅可见验证”和“不可生产”标记。

- [x] **步骤 4：实现中断恢复**

在新运行开始前以及通过 CLI 命令，将缺少终态的 `run_started` 事件与暂存和最终目录核对。幂等地追加一条 `interrupted` 事件，并保留全部孤立字节用于审计。

- [x] **步骤 5：验证并提交**

运行：`uv run python -m unittest tests.test_workflow tests.test_ledger tests.test_cli -v`

预期：通过。

提交：`feat: atomically publish auditable runs`

### 任务 7：完成合成验收、迁移指南和服务器重跑闸门

**文件：**

- 修改：`tests/test_synthetic_e2e.py`
- 修改：`README.md`
- 修改：`docs/START_HERE.md`
- 新建：`docs/migrations/v0-to-v0.1.md`
- 新建：`docs/acceptance/v0.1-server-evidence-template.md`

**接口：**

- 产出：可复现的 V0.1 验收和不含敏感信息的服务器证据锚点。
- 依赖：完整的可信可见验证工作流。

- [x] **步骤 1：扩展合成端到端测试**

覆盖政策和研究族登记、结果前失败时结果访问次数为零、工作日预热、动态泄漏拒绝、结构和输出重复、参考因子缺失、完整产物哈希以及中断运行恢复。继续只使用程序生成的数据。

- [x] **步骤 2：运行完整本地测试套件**

运行：`uv run python -m unittest discover -s tests -v`

预期：全部测试通过，真实数据访问次数保持为零。

- [x] **步骤 3：记录显式迁移方法**

解释为什么不能复用 V0 批次和候选 ID，如何依次登记政策、研究族、候选和批次，以及如何将第一次 V0 可见运行保留为 `legacy_untrusted_visible`，但不让它成为 V0.1 参考。

- [x] **步骤 4：提交本地 V0.1 完成状态**

提交：`docs: complete factor miner v0.1 local acceptance`

- [x] **步骤 5：运行公司服务器验收**

在获批的 Linux 服务器上，核验干净提交和实际清单及配置哈希，运行诊断命令，运行两次有界且只读输入的冒烟测试，登记新批准的 V0.1 研究族、政策和批次，运行一次可见验证，再校验账本，并且只填写不含敏感信息的证据模板。不得将真实数据行、完整指标、含密路径或产物复制进 Git。

- [x] **步骤 6：标记 V0.1 完成**

只有服务器测试和证据哈希核验通过后，才能勾选最后一步并更新 README 状态。可见验证的统计结果可以通过，也可以失败；版本完成要求运行可信且终态明确，而不是必须得到正向 Alpha 结果。
