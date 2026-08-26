# 阶段 B：DeepSeek 表达式来源接入实施计划（Implementation Plan）

> **面向执行代理：**执行本计划时必须逐任务使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`。每一步使用复选框跟踪。

**目标：**在阶段 A 的固定候选 Pilot 已经真实发布后，把候选来源从本地手工文件替换为“已批准假设 + DeepSeek 响应”，并让所有响应先经过本地硬校验，再复用阶段 A 的同一计算、回测、发布和投影链路。

**架构：**新增 `DeepSeekExpressionProvider`，只负责发出脱敏的假设和字段能力请求、记录不可变请求/响应摘要、把响应规范化成三个 `CandidateFactorSpec`。它不执行因子计算、不接收 IC/收益/Sharpe/回撤/Barra、不改变阶段 A 的 `run_fixed_pilot`；阶段 B 的最终入口只是把 provider 产物交给阶段 A。

**技术栈：**现有 `llm_provider`、`llm_privacy`、`llm_online`、`llm_candidate`、typed AST、Pydantic、阶段 A Pilot runner、现有服务器私有配置和 PostgreSQL 投影。

## 全局约束

- 阶段 A 的验收必须已经完成；阶段 A 的表达式、统计口径、产物 Schema 和 Dashboard 不因接入 DeepSeek 而分叉。
- DeepSeek 只能看到脱敏的已批准假设、字段能力、允许操作和输出 Schema；不能看到 QuantLake 原始数据、个股样本、IC、收益、Sharpe、回撤、Barra 或完整结果。
- 每个已批准假设生成恰好三个表达式；本阶段不扩展到 120 槽，不生成 generation seal，不让模型控制评价政策或预算。
- Schema、字段白名单、typed AST、lookback、可得性和编译检查是阻塞执行闸门；semantic lint 保留为非阻塞诊断。
- 旧的 `llmfamily_1bae19965638a6ac9620e0b0` 不作为本阶段输入；应建立新的、与阶段 B 运行绑定的身份。
- 本地真实结果仍只在公司 Linux 计算；Mac 只使用录制的合成响应测试。

---

## 文件结构

- 创建：`src/factor_miner/pilot_llm.py`，定义 DeepSeek 输出合同、请求边界、响应规范化和候选来源适配器。
- 修改：`src/factor_miner/llm_provider.py`，仅补充阶段 B 所需的 provider 调用和脱敏请求摘要，不改变既有 family 账本语义。
- 修改：`src/factor_miner/cli.py`，增加 `factor-miner pilot run-approved`，并复用阶段 A 的 `run_fixed_pilot`。
- 创建：`tests/fixtures/pilot/deepseek_expression_response.json`，保存不含真实结果的录制合成响应。
- 创建：`tests/test_pilot_llm.py`，测试响应 Schema、脱敏、硬闸门和三个候选规范化。
- 创建：`tests/test_pilot_llm_integration.py`，测试 provider 输出与阶段 A 输入的等价性。
- 修改：`docs/contracts/v1自动化研究与可视化运行合同.md`，阶段 B 验收后补充批准假设到 Pilot 的命令和失败恢复规则。

## 核心接口

```python
class DeepSeekExpressionProvider(Protocol):
    def generate_three(
        self,
        request: ExpressionGenerationRequest,
    ) -> ExpressionGenerationResponse: ...


def normalize_expression_response(
    response: ExpressionGenerationResponse,
    hypothesis: HypothesisSpec,
    field_registry: FieldRegistry,
) -> tuple[CandidateFactorSpec, CandidateFactorSpec, CandidateFactorSpec]: ...


def run_approved_pilot(
    request: ApprovedPilotRequest,
    provider: DeepSeekExpressionProvider,
    sources: PilotSources,
    artifact_root: Path,
) -> PilotRunResult: ...
```

`ExpressionGenerationRequest` 只包含 `request_id`、已批准假设的公开文本、字段能力摘要、允许操作、最大 lookback、输出 Schema 和三个固定槽位 ID。`ExpressionGenerationResponse` 必须包含 `provider_call_id`、模型标识、原始响应哈希、三个带 AST 的设计和非阻塞 lint 诊断；响应正文写入服务器私有产物，不进入 Git。

## 分任务实施步骤

### 任务 1：冻结 DeepSeek 输入输出合同

**文件：**
- 创建：`src/factor_miner/pilot_llm.py`
- 创建：`tests/test_pilot_llm.py`
- 创建：`tests/fixtures/pilot/deepseek_expression_response.json`

- [ ] **步骤 1：写失败测试。** 测试请求不含行情、结果和政策可调字段；响应必须恰好包含三个设计；缺少 AST、字段、lookback 或槽位时拒绝。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_pilot_llm.py -v`；预期阶段 B 合同类型尚未存在。
- [ ] **步骤 3：实现冻结 Pydantic 合同。** 对请求、响应、设计、lint 诊断和调用摘要设置 `extra="forbid"` 与不可变模型；把完整请求/响应 SHA-256 记录到服务器私有运行目录。
- [ ] **步骤 4：运行测试确认通过。** 运行 `pytest tests/test_pilot_llm.py -v`；录制响应不包含任何真实因子或回测结果。
- [ ] **步骤 5：提交独立变更。** 提交阶段 B 的输入输出合同。

### 任务 2：实现脱敏请求和 provider 调用

**文件：**
- 修改：`src/factor_miner/llm_provider.py`
- 修改：`src/factor_miner/llm_privacy.py`
- 修改：`src/factor_miner/pilot_llm.py`
- 修改：`tests/test_pilot_llm.py`

- [ ] **步骤 1：写失败测试。** 给请求注入个股代码、原始数值、IC、收益、Sharpe、回撤和 Barra 字段，验证脱敏扫描拒绝；验证只保留字段能力和批准假设公开文本。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_pilot_llm.py -k privacy -v`；预期阶段 B 请求适配尚未接入。
- [ ] **步骤 3：实现最小 provider。** 复用现有 `llm_provider` 的授权和调用端口；模型只返回结构化表达式设计，程序固定 `max_hypotheses=1`、设计数量为 3、调用次数受服务器配置限制。
- [ ] **步骤 4：实现超时和外部失败语义。** provider 不可用时保留不可变请求摘要和失败状态，不调用阶段 A；恢复时只能重放同一请求哈希，不能把“重试”变成新的研究想法。
- [ ] **步骤 5：运行测试确认通过。** 运行 `pytest tests/test_pilot_llm.py -k privacy -v`；预期所有结果字段均会被禁止外发。
- [ ] **步骤 6：提交独立变更。** 提交脱敏调用层。

### 任务 3：实现本地硬校验和候选规范化

**文件：**
- 修改：`src/factor_miner/pilot_llm.py`
- 修改：`src/factor_miner/field_registry.py`（仅复用现有字段能力接口，不降低白名单要求）
- 创建：`tests/test_pilot_llm.py` 中的校验测试

- [ ] **步骤 1：写失败测试。** 覆盖未知字段、未知操作、`center=true`、负周期、超出 lookback、future/label 字段、`eval`/`exec` 字样、required fields 不一致和编译失败。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_pilot_llm.py -k validate -v`；预期响应尚未经过阶段 A 同等 compiler。
- [ ] **步骤 3：实现硬校验顺序。** 依次执行响应 Schema、设计数量/槽位、字段注册表、`FactorNode`、`compile_candidate`、lookback、可得性和输入合同；任何一项失败都不产生可执行候选。
- [ ] **步骤 4：实现非阻塞 semantic lint。** 保存 lint 诊断、风险标签和模型解释，但 lint 不修改 AST、不改变候选计数，也不阻塞已通过硬校验的执行。
- [ ] **步骤 5：运行测试确认通过。** 运行 `pytest tests/test_pilot_llm.py -k validate -v`；恶意或不合法响应被拒绝，合法响应得到三个确定性 `CandidateFactorSpec`。
- [ ] **步骤 6：提交独立变更。** 提交本地候选规范化层。

### 任务 4：把批准假设接入阶段 A

**文件：**
- 修改：`src/factor_miner/pilot_llm.py`
- 修改：`src/factor_miner/cli.py`
- 创建：`tests/test_pilot_llm_integration.py`

- [ ] **步骤 1：写失败测试。** 用录制响应调用 `run_approved_pilot`，验证下游只收到三个规范化 Spec，且生成的 `portfolio/metrics.json`、`ic/diagnostics.json`、`barra/attribution.json` 和 `run_manifest.json` Schema 与阶段 A 完全一致。
- [ ] **步骤 2：运行测试确认失败。** 运行 `pytest tests/test_pilot_llm_integration.py -v`；预期 `pilot run-approved` 尚未能复用阶段 A。
- [ ] **步骤 3：实现编排。** `run_approved_pilot` 先完成假设批准检查、脱敏调用和本地硬校验，再将三个 Spec 传入 `run_fixed_pilot` 的同一入口；不得复制因子计算、IC 或回测实现。
- [ ] **步骤 4：实现 CLI。** 增加 `factor-miner pilot run-approved --hypothesis ... --field-registry ... --artifact-root ...`；命令输出 provider call ID、候选 ID、pilot_run_id 和发布路径，但不打印原始响应或真实数据。
- [ ] **步骤 5：运行测试确认通过。** 运行 `pytest tests/test_pilot_llm_integration.py -v`；替换候选来源不会改变下游产物字段、政策 ID 或统计协议。
- [ ] **步骤 6：提交独立变更。** 提交阶段 B CLI 和阶段 A 复用。

### 任务 5：服务器真实批准假设验收

**文件：**
- 创建：`docs/runbooks/阶段B批准假设生成三因子并运行Pilot.md`
- 修改：`docs/contracts/v1自动化研究与可视化运行合同.md`

- [ ] **步骤 1：创建新的阶段 B 运行身份。** 不使用污染 family，不把阶段 B 结果写入旧 family 事件账本。
- [ ] **步骤 2：检查外发范围。** 在服务器完成批准假设、字段能力和 provider scope 检查；请求正文只包含公开信息和字段能力。
- [ ] **步骤 3：执行 `factor-miner pilot run-approved`。** provider 响应通过本地硬校验后进入阶段 A；失败时保留调用摘要和失败原因，不生成伪候选。
- [ ] **步骤 4：核验结果和风险边界。** 即使三个生成因子全部差，也必须生成完整 Pilot 诊断；报告只能说候选诊断已发布，不能说因子有效。
- [ ] **步骤 5：运行 PostgreSQL 投影和 Dashboard。** 继续沿用阶段 A 的发布后投影和可重试机制。

## 阶段 B 明确验收标准

1. 一条批准假设能稳定生成恰好三个可执行 `CandidateFactorSpec`，每个均通过字段白名单、typed AST、lookback、可得性和 compiler。
2. DeepSeek 请求不含真实行情、个股样本、IC、收益、Sharpe、回撤和 Barra 结果；响应不会被发送回模型进行自我修复。
3. semantic lint 仍有诊断产物，但不是主执行阻塞点；非法 AST 仍由本地硬闸门阻止。
4. 阶段 B 与阶段 A 共享同一因子计算、IC、回测、成本、日历、Barra、发布和 PostgreSQL 投影代码。
5. provider 故障不会污染阶段 A 产物；同一请求可按请求哈希恢复，重新投影不重复生成研究想法。
6. `factor-miner pilot run-approved` 可以在服务器上一句话完成“批准假设 → 生成三个候选 → 运行 Pilot → 发布 → 可选投影”，但仍不代表 120 槽正式研究族已经运行。

阶段 B 完成后，才允许执行阶段 C；正式 family 账本和 generation seal 的修复必须集中在阶段 C 处理。
