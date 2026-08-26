# 阶段 C：正式研究族与 120 槽治理恢复实施计划（Implementation Plan）

> **面向执行代理：**执行本计划时必须逐任务使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`。每一步使用复选框跟踪。

**目标：**在阶段 A 固定候选链路和阶段 B DeepSeek 入口均已真实验收后，恢复 10 个假设 × 每个假设 3 个因子、四条研究 arm、总计 120 槽、不可变事件账本、generation seal、HAC/Bonferroni 多重检验和正式运行发布。

**架构：**正式治理层只负责研究族、槽位、请求/响应、候选状态、封存和统计预算；实际因子计算、IC、组合、Barra、发布和 PostgreSQL 投影继续调用阶段 A 的单一 Pilot evaluator。新治理层必须先完成事件合法性预检再追加账本，污染的旧 family 永久隔离，不能通过补写事件修复。

**技术栈：**现有 `llm_ledger`、`llm_state`、`llm_seal`、`shadow_generators`、`statistics`、阶段 A Pilot runner、阶段 B provider、Parquet/JSONL 正式主存储、PostgreSQL 投影和只读 Dashboard。

## 全局约束

- 只有阶段 A 和阶段 B 的服务器验收记录完整后，才可开始本阶段。
- 每次正式研究族固定 10 个假设；每个假设每条 arm 占 3 个槽；四条 arm 共 120 槽：`coverage_outcome_llm`、`literature_only_llm`、`mechanical_mutation`、`hypothesis_conditioned_grammar`，每条 30 槽。
- `llmfamily_1bae19965638a6ac9620e0b0` 永久标记为审计失败样本；不修复、不删除、不重跑、不把它作为新运行的父状态。
- 新 family 必须使用新 `family_id`、新目录和新内容哈希；状态投影发现旧账本非法时必须隔离并停止该 family。
- 事件账本只追加；任何非法状态转移在写入前拒绝，不能先写再依靠回放时报错。
- 所有候选和参数变体必须在读取 IC、p 值、收益或相关性前登记；失败、中断、重复、人工否决和基础设施终止均保留显式终态。
- generation seal 之前禁止打开结果端口；seal 之后评价协议、掩码、标签、切分、HAC 参数、Bonferroni 分母和冗余阈值仍不可修改。
- DSR 不能代替因子 IC 多重检验；RankIC 显著性使用冻结 HAC 和 Bonferroni 校正。
- 正式结果仍只能称为通过冻结可见协议计算的候选因子诊断，不构成因果机制认证或实盘建议。

---

## 文件结构

- 修改：`src/factor_miner/llm_ledger.py`，在 `_HashChainJsonlStore.append` 之前完成状态转移预检和写入互斥检查。
- 修改：`src/factor_miner/llm_state.py`，集中定义 120 槽状态机、合法终态和 seal 前后可见性。
- 修改：`src/factor_miner/llm_seal.py`，从服务器本地槽对象重算 seal 输入、覆盖 120 槽和完整 hash。
- 修改：`src/factor_miner/llm_orchestrator.py`，把已批准假设的三个表达式送入阶段 B，再把合法候选送入阶段 A，不重复计算。
- 修改：`src/factor_miner/shadow_generators.py`，为两个 shadow arm 生成显式槽位和无 outcome 的终态。
- 创建：`src/factor_miner/research_campaign_runner.py`，只编排“登记 → seal → 评价 → 发布”，不复制金融计算器。
- 修改：`src/factor_miner/cli.py`，增加 `campaign run-approved`、`campaign seal`、`campaign evaluate` 和 `campaign verify`，并拒绝旧污染 family。
- 创建：`tests/test_llm_ledger_transitions.py`，测试非法转移在写入前失败。
- 创建：`tests/test_research_campaign_120_slots.py`，测试四条 arm 的 120 槽完整性和中断恢复。
- 创建：`tests/test_research_campaign_evaluation.py`，测试 seal 可见性、HAC、Bonferroni 和正式 Pilot 发布。
- 创建：`docs/runbooks/正式研究族120槽服务器运行.md`，记录新 family 的服务器操作和审计检查。

## 核心接口

```python
def append_validated_event(
    self,
    family_id: str,
    event: LLMEvent,
) -> LLMEvent: ...


def validate_transition(
    current: ProjectedFamilyState,
    event: LLMEvent,
) -> None: ...


def build_generation_seal(
    family: RegisteredLLMDiscoveryResearchFamily,
    slots: tuple[RegisteredCandidateSlot, ...],
    policy: FrozenEvaluationPolicy,
) -> RegisteredGenerationSeal: ...


def evaluate_sealed_campaign(
    seal: RegisteredGenerationSeal,
    campaign: RegisteredLLMDiscoveryResearchFamily,
    pilot_runner: PilotRunner,
) -> CampaignEvaluationResult: ...
```

`evaluate_sealed_campaign` 必须对每个已登记候选调用阶段 A 的统一 evaluator；它不得自行实现另一套标签、分组、成本、日历、Barra 或发布逻辑。

## 分任务实施步骤

### 任务 1：重建可验证的状态转移边界

**文件：**
- 修改：`src/factor_miner/llm_ledger.py`
- 修改：`src/factor_miner/llm_state.py`
- 创建：`tests/test_llm_ledger_transitions.py`

- [ ] **步骤 1：写失败测试。** 构造 `generation_in_progress → schema_failed` 之类非法转移，验证事件文件字节数和行数在异常后完全不变；验证合法转移仍能追加并重放。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_llm_ledger_transitions.py -v`；预期当前 append 先写后验的问题会被测试捕获。
- [ ] **步骤 3：实现预检追加。** 在 `_HashChainJsonlStore.append` 之前读取并校验当前投影、事件 family、事件序号、前一哈希和合法状态转移；全部通过后才在单写入器锁内追加一行。
- [ ] **步骤 4：实现隔离语义。** 发现现有 family 已有非法历史时返回 `AUDIT_QUARANTINED`，禁止任何修复事件、继续生成或 seal；新 family 不继承旧目录。
- [ ] **步骤 5：运行测试确认通过。** 运行 `pytest tests/test_llm_ledger_transitions.py -v`；非法事件不落盘，合法事件可重放，损坏 family 只读隔离。
- [ ] **步骤 6：提交独立变更。** 提交账本边界修复。

### 任务 2：实现 120 槽注册和中断恢复

**文件：**
- 修改：`src/factor_miner/llm_state.py`
- 修改：`src/factor_miner/llm_orchestrator.py`
- 修改：`src/factor_miner/shadow_generators.py`
- 创建：`tests/test_research_campaign_120_slots.py`

- [ ] **步骤 1：写失败测试。** 验证 10 个批准假设各自产生 3 个主设计；四条 arm 组合后恰好 120 个唯一槽位；中断后只恢复相同请求哈希，不生成新设计。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_research_campaign_120_slots.py -v`；预期当前编排无法为所有 arm 生成完整可重放槽位。
- [ ] **步骤 3：实现槽位登记。** 把 `family_id、arm_id、hypothesis_id、slot_id、request_hash、candidate_hash、状态` 写入不可变对象；每个槽在读取任何结果前登记。
- [ ] **步骤 4：实现 shadow 终态。** `mechanical_mutation` 和 `hypothesis_conditioned_grammar` 只使用已登记的 coverage 父候选和固定程序规则，不读取 outcome；无批准假设或无合格父候选时写 `not_executed` 终态。
- [ ] **步骤 5：实现 resume。** 读取同一 slot 的不可变请求/响应/候选对象；已完成槽跳过 provider，失败槽按原请求哈希恢复，禁止“重试”改变候选内容。
- [ ] **步骤 6：运行测试确认通过。** 运行 `pytest tests/test_research_campaign_120_slots.py -v`；预期得到 120 个唯一槽和可重复的中断恢复结果。
- [ ] **步骤 7：提交独立变更。** 提交槽位和恢复逻辑。

### 任务 3：实现 generation seal 和 seal 前结果隔离

**文件：**
- 修改：`src/factor_miner/llm_seal.py`
- 修改：`src/factor_miner/llm_state.py`
- 修改：`src/factor_miner/cli.py`
- 创建：`tests/test_research_campaign_120_slots.py` 中的 seal 测试

- [ ] **步骤 1：写失败测试。** 测试 119 槽、重复槽、缺终态、hash 不一致和候选对象缺失时 seal 失败；测试未 seal 时调用评价器硬失败。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_research_campaign_120_slots.py -k seal -v`；预期当前 seal 流程不能覆盖新的完整槽合同。
- [ ] **步骤 3：实现本地重算 seal。** 从服务器本地槽对象排序后重算槽哈希、campaign hash、评价政策 hash、统计预算 hash 和数据合同身份，生成不可变 `generation_seal_id`。
- [ ] **步骤 4：实现结果端口。** `campaign evaluate` 只接受已经验证的 seal；seal 之前不读取因子值、IC、p 值、收益或 Barra 结果。
- [ ] **步骤 5：运行测试确认通过。** 运行 `pytest tests/test_research_campaign_120_slots.py -k seal -v`；完整 120 槽才可 seal，seal 内容改变会产生新的 seal ID。
- [ ] **步骤 6：提交独立变更。** 提交 seal 和结果可见性。

### 任务 4：接入正式多重检验和统一 Pilot evaluator

**文件：**
- 创建：`src/factor_miner/research_campaign_runner.py`
- 修改：`src/factor_miner/statistics.py`
- 创建：`tests/test_research_campaign_evaluation.py`

- [ ] **步骤 1：写失败测试。** 测试所有登记候选都进入统计分母；RankIC HAC t 统计量使用冻结参数；Bonferroni 校正按完整 120 槽/预登记族计算；失败候选仍保留状态。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_research_campaign_evaluation.py -v`；预期正式 campaign 尚未调用阶段 A evaluator。
- [ ] **步骤 3：实现统一评价编排。** seal 验证后逐槽调用阶段 A 的 `run_fixed_pilot` 内部候选执行函数，复用同一真实日历、标签、分组、成本、CSI300、Barra 和发布合同；只增加 campaign/family 维度的元数据。
- [ ] **步骤 4：实现统计汇总。** 对 IC、RankIC、t 值、阈值概率、IC decay、冗余和多重检验保存完整分母；DSR 只作为附加诊断，不能替代 IC 显著性。
- [ ] **步骤 5：实现冗余顺序。** 先比较规范 AST/AST hash，再比较可见区间逐日截面 Spearman 输出相关性；高冗余候选保留结果和失败原因，不从分母移除。
- [ ] **步骤 6：运行测试确认通过。** 运行 `pytest tests/test_research_campaign_evaluation.py -v`；坏因子可以发布为失败诊断，但不得因统计结果差而删槽或改政策。
- [ ] **步骤 7：提交独立变更。** 提交正式评价汇总。

### 任务 5：恢复正式 CLI、发布和 Dashboard 连接

**文件：**
- 修改：`src/factor_miner/cli.py`
- 修改：`src/factor_miner/dashboard_projection.py`
- 修改：`dashboard/pages/1_批次总览.py`
- 创建：`tests/test_research_campaign_evaluation.py` 中的发布测试

- [ ] **步骤 1：写失败测试。** 测试 `campaign evaluate` 在没有 seal 时拒绝；有 seal 时生成统一 `run_manifest.json`，并能将 family、seal、候选、IC、组合、Barra 和统计预算展示到只读 Dashboard。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_research_campaign_evaluation.py -k publish -v`；预期 campaign 元数据尚未进入运行产物和读模型。
- [ ] **步骤 3：实现发布元数据。** 在阶段 A 产物上追加 family、seal、slot、统计族、完整分母和失败槽摘要；不把完整原始行情、响应正文或密钥复制到 PostgreSQL。
- [ ] **步骤 4：实现正式 CLI。** 提供 `campaign run-approved`、`campaign seal`、`campaign evaluate`、`campaign verify`；所有命令拒绝污染 family，评价失败不删除已发布产物。
- [ ] **步骤 5：运行测试确认通过。** 运行 `pytest tests/test_research_campaign_evaluation.py -k publish -v` 和现有 Dashboard 合同测试。
- [ ] **步骤 6：提交独立变更。** 提交正式发布和展示入口。

### 任务 6：120 槽 Linux 真实演练和治理验收

**文件：**
- 创建：`docs/runbooks/正式研究族120槽服务器运行.md`
- 修改：`docs/contracts/v1自动化研究与可视化运行合同.md`

- [ ] **步骤 1：创建全新的正式 family。** 记录新的 `family_id`、审批输入、scope、字段注册表、政策、代码提交和数据版本；旧污染 family 保持字节不变。
- [ ] **步骤 2：运行 10 个假设 × 3 个主设计。** 每个假设完成 3 个候选注册；provider 失败、硬校验失败、人工否决和基础设施终止都写入槽终态。
- [ ] **步骤 3：运行两条 shadow arm。** 生成完整 120 槽状态；只使用冻结程序规则，不读取结果。
- [ ] **步骤 4：执行 generation seal。** 核验 120 个唯一槽、所有终态、哈希、政策和数据身份；seal 前不得读取结果。
- [ ] **步骤 5：执行正式评价。** 调用统一 Pilot evaluator，完成 IC/RankIC、多重检验、十组、多空、成本、超额收益和可选 Barra。
- [ ] **步骤 6：发布并投影。** 写不可变 `run_manifest.json`，随后独立投影 PostgreSQL；数据库失败时进行重试，不回滚服务器产物。
- [ ] **步骤 7：完成最终审计。** 核对旧 family 未修改、新 family 120 槽完整、seal 可重算、结果可重放、Dashboard 只读、所有失败槽可追踪。

## 阶段 C 明确验收标准

1. 新正式 family 有 10 个批准假设、每个假设 3 个主设计、四条 arm 共 120 个唯一槽，所有槽均有明确终态。
2. 非法状态转移在追加前被拒绝，账本不会再次产生“先写入、回放才发现不合法”的污染；旧污染 family 仍只读隔离。
3. 没有完整 120 槽和有效 generation seal，任何 IC、回测、Barra 或晋级结果都不可见。
4. seal 后的评价严格使用冻结统计政策、HAC、Bonferroni、多重检验分母和冗余协议；失败候选不从分母消失。
5. 正式研究族与固定 Pilot 使用同一个下游金融计算器和产物合同，不能因为引入 family 形成第二套回测逻辑。
6. 正式运行能发布本地不可变产物、可幂等投影 PostgreSQL、由 Dashboard 只读展示，并能从运行清单和槽对象完成审计重放。

完成阶段 C 后，才算实现“批准假设 → 自动生成三个因子 → 本地硬校验 → 服务器回测统计 → 发布入库 → Dashboard 展示”的完整正式流程；任何统计结果仍不能被解释成因果证明或实盘收益承诺。
