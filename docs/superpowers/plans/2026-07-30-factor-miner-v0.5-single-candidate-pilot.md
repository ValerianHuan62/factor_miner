# V0.5 单候选在线试运行实施计划

> **给代理开发者：** 必须使用 `superpowers:executing-plans` 按任务执行；每一步用复选框跟踪。这里的三个代理是 Factor Miner 运行时研究角色，不是开发代理。

**目标：** 在不向 DeepSeek 泄露公司数据、个人信息、真实因子身份或密钥的前提下，完成一次三角色在线生成，并把至少一个合法候选接入既有服务器可见评价链路。

**架构：** 固定程序先从 V0.4 图谱构造脱敏 brief，并对精确请求字节做政策与人工哈希授权；DeepSeek 只能通过固定官方端点工作。代理 1 仅能调用受限公开文献网关，代理 2 只生成三个公开别名 AST，代理 3 只做无证据权重的语义一致性检查。所有响应先逐字录制，再由固定程序完成字段映射、availability、单位类型、DSL、编译、登记和 generation seal；评价端口在 seal 核验前不可打开。

**技术栈：** Python 3.12、Pydantic 2、标准库 `urllib`、Typer、现有 Factor Miner JSON/JSONL 账本、DeepSeek OpenAI 兼容 Chat Completions。

## 全局约束

- 正式端点固定为 `https://api.deepseek.com/chat/completions`，模型固定为 `deepseek-v4-pro`，`thinking=enabled`、`reasoning_effort=high`、`stream=false`、`response_format=json_object`。
- `DEEPSEEK_API_KEY` 只能由调用函数从当前进程环境读取，不得出现在 CLI 参数、异常、请求录制、日志或环境快照。
- 真实 brief、外部调用、字段注册表和评价只允许在公司 Linux；Mac 只运行注入假客户端的纯合成测试。
- 代理 1 只获得脱敏 brief 和 `search_literature`；代理 2、代理 3 不得获得任何工具。
- 文献网关首个适配器固定使用 Crossref 官方 REST 元数据接口，不访问模型提供的 URL，不抓取网页正文。
- 单候选试运行是操作演练，不改变 120 槽统计预算。未执行槽仍必须进入明确终态；Bonferroni 分母保持 120。
- 外发前必须展示规范 payload 的 SHA-256、字节数、信息类别、模型和预算，并由用户批准相同哈希；任何字节变化使授权失效。
- 结果只能称为“通过可见验证的候选因子”或相应失败终态，不能称为认证因子、有效 Alpha 或生产结论。

---

### 任务 1：冻结在线调用、授权和录制合同

**文件：**
- 新建：`src/factor_miner/llm_online.py`
- 修改：`src/factor_miner/errors.py`
- 测试：`tests/test_llm_online.py`

**接口：**
- 消费：`scan_export_payload(payload, policy) -> bytes`
- 产出：`DeepSeekRequestSpec`、`LLMExportAuthorization`、`LLMCallRecord`、`build_deepseek_request(...)`、`verify_export_authorization(...)`

- [x] **步骤 1：先写失败测试**

测试固定 endpoint、model、thinking、JSON mode 和 role token 上限；修改 payload、prompt、model 或预算后旧授权失效；Schema 不存在 `api_key`、`base_url` 可覆盖字段。

- [x] **步骤 2：运行测试确认失败**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest tests.test_llm_online -v
```

预期：因 `factor_miner.llm_online` 不存在而失败。

- [x] **步骤 3：实现最小合同**

```python
def build_deepseek_request(
    *,
    campaign_id: str,
    agent_role: AgentRole,
    slot_ids: tuple[str, ...],
    system_prompt: str,
    user_payload: dict[str, object],
    tools: tuple[dict[str, object], ...] = (),
) -> PreparedDeepSeekRequest: ...

def verify_export_authorization(
    request: PreparedDeepSeekRequest,
    policy: CorporateExternalResearchPolicy,
    authorization: LLMExportAuthorization,
    now: datetime,
) -> bytes: ...
```

函数先做政策扫描，再比较精确规范请求哈希、policy ID、campaign ID、有效期、模型和角色；不读取密钥。

- [x] **步骤 4：运行测试并提交**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest tests.test_llm_online -v
git add src/factor_miner/llm_online.py src/factor_miner/errors.py tests/test_llm_online.py
git commit -m "feat: freeze controlled deepseek requests"
```

### 任务 2：实现逐字录制、重放和密钥安全的 DeepSeek 客户端

**文件：**
- 新建：`src/factor_miner/llm_provider.py`
- 测试：`tests/test_llm_provider.py`

**接口：**
- 消费：任务 1 的 `PreparedDeepSeekRequest` 和 `LLMExportAuthorization`
- 产出：`DeepSeekTransport` 协议、`execute_recorded_call(...) -> RecordedLLMResponse`、`replay_recorded_call(...)`

- [x] **步骤 1：先写协议失败测试**

注入假 transport，断言实际发送体与授权体逐字相同；响应原始字节、SHA-256、usage、finish reason 和 system fingerprint 原子保存。测试 401、429、5xx、空 content、非法 JSON 和篡改重放；异常与产物均不得出现测试密钥。

- [x] **步骤 2：运行测试确认失败**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest tests.test_llm_provider -v
```

- [x] **步骤 3：实现固定传输**

```python
class DeepSeekTransport(Protocol):
    def post(self, request_bytes: bytes, api_key: str) -> bytes: ...

def execute_recorded_call(
    prepared: PreparedDeepSeekRequest,
    authorization: LLMExportAuthorization,
    policy: CorporateExternalResearchPolicy,
    record_root: Path,
    transport: DeepSeekTransport,
) -> RecordedLLMResponse: ...
```

默认 transport 只 POST 固定官方 endpoint，超时固定 120 秒。密钥在发送前才从 `DEEPSEEK_API_KEY` 读取；缺失、空值和 HTTP 错误只返回稳定中文失败码，不包含 header 或响应中的潜在密钥。

- [x] **步骤 4：运行测试并提交**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest tests.test_llm_provider -v
git add src/factor_miner/llm_provider.py tests/test_llm_provider.py
git commit -m "feat: record and replay deepseek calls"
```

### 任务 3：实现受限 Crossref 文献网关与代理 1 工具循环

**文件：**
- 新建：`src/factor_miner/llm_literature.py`
- 新建：`src/factor_miner/llm_agents.py`
- 测试：`tests/test_llm_literature.py`
- 测试：`tests/test_llm_agents.py`

**接口：**
- 产出：`LiteratureQuery`、`LiteratureSearchRecord`、`CrossrefMetadataAdapter.search(query) -> LiteratureSearchRecord`
- 产出：`run_hypothesis_agent(...) -> tuple[CoverageGapHypothesisDraft, ...]`

- [x] **步骤 1：先写文献边界测试**

只允许 `query_terms/year_start/year_end/result_limit`；拒绝 URL、IP、控制字符、超预算和非公开字段词。固定程序构造 Crossref 查询 URL，响应只保留 DOI、标题、作者、年份、摘要片段、期刊和规范 URL；不跟随结果 URL。

- [x] **步骤 2：先写代理权限测试**

代理 1 请求恰好注册一个 `search_literature` 工具；工具参数先校验再调用 adapter；工具结果被包裹为不可执行元数据。代理 2 和代理 3 的请求 `tools=()`。隐藏 `reasoning_content` 不进入正式响应产物。

- [x] **步骤 3：实现并运行测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest tests.test_llm_literature tests.test_llm_agents -v
```

- [x] **步骤 4：提交**

```bash
git add src/factor_miner/llm_literature.py src/factor_miner/llm_agents.py tests/test_llm_literature.py tests/test_llm_agents.py
git commit -m "feat: add bounded literature hypothesis agent"
```

### 任务 4：实现字段可用性、DSL 语义类型和候选转换

**文件：**
- 新建：`src/factor_miner/field_registry.py`
- 新建：`src/factor_miner/dsl_semantics.py`
- 新建：`src/factor_miner/llm_candidate.py`
- 测试：`tests/test_field_registry.py`
- 测试：`tests/test_dsl_semantics.py`
- 测试：`tests/test_llm_candidate.py`

**接口：**
- 产出：`FieldAvailabilityRegistry`、`analyse_semantic_type(node, registry) -> SemanticAnalysis`
- 产出：`generate_candidate_drafts(...)`、`lint_candidates(...)`、`convert_candidate(...) -> TrustedCandidateFactorSpec`

- [x] **步骤 1：先写字段与单位失败测试**

非 point-in-time 字段、评价期后才可用字段和 `eligible_for_factor=false` 必须失败；`price + traded_value` 失败，`delta(price)/delay(price)` 输出无量纲，rolling 保持单位，`rolling_corr` 输出无量纲。

- [x] **步骤 2：先写候选转换测试**

公开别名只能由本地注册表映射；`required_fields`、lookback、availability 和 candidate ID 全由固定程序推导。代理 3 只能批准或拒绝，不能修改 AST；未声明暴露、未知算子、未来字段和重复 AST 在 outcome 前终止。

- [x] **步骤 3：实现并运行测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest tests.test_field_registry tests.test_dsl_semantics tests.test_llm_candidate -v
```

- [x] **步骤 4：提交**

```bash
git add src/factor_miner/field_registry.py src/factor_miner/dsl_semantics.py src/factor_miner/llm_candidate.py tests/test_field_registry.py tests/test_dsl_semantics.py tests/test_llm_candidate.py
git commit -m "feat: compile llm drafts into trusted candidates"
```

### 任务 5：接入在线试运行 CLI 和服务器操作演练

**文件：**
- 修改：`src/factor_miner/cli.py`
- 新建：`tests/test_llm_pilot_cli.py`
- 新建：`docs/contracts/v0.5单候选在线试运行.md`
- 修改：`README.md`

**接口：**
- 产出：`llm export-preview`、`llm authorize-export`、`llm pilot-hypothesis`、`llm pilot-candidates`、`llm pilot-verify`

- [x] **步骤 1：写 CLI 失败测试**

没有 policy、授权、密钥或 Linux 身份时真实调用失败；CLI 不暴露 key、endpoint、model、跳过隐私、自动批准、缩小 family、跳过 seal 或重抽参数。`pilot-hypothesis` 完成后必须停在人工假设审阅闸门。

- [x] **步骤 2：实现 CLI 与中文运行合同**

操作演练 family 仍登记 120 槽。用户只批准一个假设时，固定程序把未使用假设与基线槽写入明确未执行终态；代理 2 的三个候选槽照常消耗，只有固定程序与代理 3 均通过的候选进入现有登记链路。任何评价前先核验 120 槽 seal。

- [x] **步骤 3：本地与服务器回归**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest discover -s tests -v
UV_CACHE_DIR=/data/factor_miner_artifacts/uv_cache uv sync --frozen
UV_CACHE_DIR=/data/factor_miner_artifacts/uv_cache uv run python -m unittest discover -s tests -v
```

- [ ] **步骤 4：服务器真实试运行**

服务器先从冻结 V0.4 图谱构建 brief，打印不含内容正文的外发预览。用户批准精确哈希并在服务器私有环境设置 `DEEPSEEK_API_KEY` 后，依次运行假设代理、人工审阅、表达式代理与语义 lint，封存 120 槽，再调用既有真实数据流程评价合法候选。

- [x] **步骤 5：记录边界并提交**

Git 只记录代码、测试、中文合同、非敏感提交号和通过/失败终态；不记录 API 响应正文、文献完整记录、真实因子、指标、账本、路径或密钥。

```bash
git add src/factor_miner/cli.py tests/test_llm_pilot_cli.py docs/contracts/v0.5单候选在线试运行.md README.md
git commit -m "feat: run controlled llm candidate pilot"
```

## 最终验收

- [x] 三个运行时代理的输入、输出与工具权限分别经过测试锁定。
- [x] DeepSeek 当前官方模型、endpoint、thinking 与 JSON 参数已按官方文档核对。
- [x] 精确外发请求只有在公司政策和用户哈希授权同时有效时才能发送。
- [x] 密钥未进入参数、日志、异常、录制、Git 或最终答复。
- [x] 服务器用精确提交和 `uv.lock` 通过全量测试。
- [ ] 至少一个候选得到明确终态；正向或负向结果都算流程跑通。
- [ ] 对用户只报告金融含义、样本边界、统计终态与风险，不泄露公司数据或个人信息。
