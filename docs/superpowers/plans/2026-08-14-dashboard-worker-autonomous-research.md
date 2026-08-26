# Dashboard 自主研究实施计划

> **代理执行要求：** REQUIRED SUB-SKILL：使用 `superpowers:executing-plans` 按任务执行；每个步骤用复选框跟踪。

**目标：** 建成由 Dashboard 启动、逐条审批十个假设、单机 Worker 自动完成批量 DeepSeek 表达式生成、真实评价、发布投影以及下一轮记忆和因子图谱刷新的自主研究流程。

**架构：** Dashboard 只向原子命令收件箱提交控制命令并读取中文状态，单机 Worker 是研究状态和正式文件账本的唯一写入者。正常批次仅调用 DeepSeek 两次；候选 manifest 和统计检验族必须在读取任何结果前冻结，真实数据计算继续复用现有公司 Linux 评价端口。

**技术栈：** Python 3.12、Pydantic 2、Typer、Streamlit、PostgreSQL 16、Polars、h5py、规范 JSON/JSONL、Linux systemd、unittest。

## 全局约束

- 正常批次固定生成十条中文假设；全部逐条批准或拒绝后才能继续。
- 拒绝的假设不补抽、不生成表达式，但必须进入下一轮记忆。
- 每条批准假设固定三个候选；统计检验族在结果揭晓前冻结为 `批准假设数 × 3`。
- 表达式、编译、数据和评价失败都保留原槽位，不补抽、不缩小 Bonferroni 分母。
- DeepSeek 不接收行情、个股样本、IC、收益、Sharpe、回撤、账本、模型或密钥。
- 正常路径只有一次十假设请求和一次批准集合表达式请求；格式修复只允许处理失败子集。
- Dashboard 不展示 TOKEN 数，不展示 DeepSeek 原始响应，也不能从摘要目录直接审批。
- “新建研究批次”命令与服务器预配置的脱敏外发政策共同构成本批范围授权；正常路径不得再出现第二个人工外发授权页面。
- PostgreSQL 字段名使用英文；所有假设、状态、错误和因子描述使用中文；候选业务编号稳定使用 `huanNNN`。
- JSON/JSONL 和不可变候选包是正式主存储；PostgreSQL 只作为 Dashboard 读模型。
- Mac 只运行纯合成测试；真实候选计算、IC、显著性和组合评价只在公司 Linux 执行。
- 保留旧 120 槽、四臂、generation seal 和记忆合同，不以修改旧常量的方式兼容轻量流程。
- `.task5-fix-round-3-tmp/` 和 `.superpowers/brainstorm/` 不进入提交。

---

### 任务 1：建立命令收件箱与中文运行状态

**文件：**

- 新建：`src/factor_miner/autonomous_schema.py`
- 新建：`src/factor_miner/research_control.py`
- 新建：`tests/test_research_control.py`

**接口：**

- 产出：`AutonomousStage`、`ResearchCommandType`、`ResearchCommand`、`ResearchStartAuthorization`、`AutonomousResearchState`。
- 产出：`ResearchControlStore.submit(command)`、`pending_commands()`、`publish_state(state)`、`load_state(run_id)`、`active_state()`。
- 存储根：`artifact_root/state/autonomous_research/`。

- [x] **步骤 1：编写原子命令和单活动批次失败测试**

```python
def test_duplicate_start_command_is_idempotent() -> None:
    command = ResearchCommand.start(requested_by="research_owner", requested_at=NOW)
    first = store.submit(command)
    second = store.submit(command)
    assert first == second
    assert len(store.pending_commands()) == 1

def test_second_active_run_is_rejected() -> None:
    store.publish_state(active_state("autrun_" + "1" * 24))
    with self.assertRaisesRegex(ValueError, "已有活动研究批次"):
        store.submit(ResearchCommand.start(requested_by="research_owner", requested_at=NOW))
```

- [x] **步骤 2：编写状态身份和中文错误测试**

断言状态哈希覆盖 `run_id`、stage、阶段引用和错误；错误文本没有中文时拒绝发布；`completed`、`no_approved_hypothesis` 和 `failed` 是终态。

- [x] **步骤 3：运行失败测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest tests.test_research_control -v
```

预期：模块不存在。

- [x] **步骤 4：实现最小 Schema 和文件 Store**

`ResearchCommand` 的身份由以下 payload 生成：

```python
payload = {
    "command_type": command_type,
    "target_run_id": target_run_id,
    "requested_by": requested_by,
    "requested_at": requested_at,
    "body": body,
}
command_id = f"researchcmd_{sha256_json(payload)[:24]}"
```

命令写入 `inbox/<command_id>.json`，状态写入 `runs/<run_id>/snapshots/<sequence>.json`。只允许 Worker 调用 `publish_state`；已存在同名不同内容时硬失败。`ResearchStartAuthorization` 绑定启动命令、允许的 `hypothesis/expression` 角色、冻结模型、审批角色和有效期；它只允许发送通过脱敏扫描的请求，不代替逐条假设审批。

- [x] **步骤 5：运行目标测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_research_control tests.test_ledger -v
```

- [x] **步骤 6：提交**

```bash
git add src/factor_miner/autonomous_schema.py \
  src/factor_miner/research_control.py tests/test_research_control.py
git commit -m "feat: add autonomous research command store"
```

---

### 任务 2：生成十条假设并冻结逐条审批

**文件：**

- 新建：`src/factor_miner/lightweight_hypotheses.py`
- 新建：`tests/test_lightweight_hypotheses.py`
- 修改：`src/factor_miner/research_evolution.py`

**接口：**

- 消费：`ResearchEvolutionContext`、脱敏 gap brief、现有 `PreparedDeepSeekRequest` 和录制 provider。
- 产出：`LightweightHypothesisBatch`、`LightweightHypothesisDecision`、`LightweightReviewBatch`。
- 产出：`generate_lightweight_hypotheses(...) -> LightweightHypothesisBatch`。
- 产出：`freeze_lightweight_review(hypotheses, decisions, ...) -> LightweightReviewBatch`。

- [x] **步骤 1：编写十条完整中文假设测试**

```python
def test_hypothesis_batch_requires_exactly_h01_to_h10() -> None:
    batch = LightweightHypothesisBatch.build(
        run_id=RUN_ID,
        context_sha256="a" * 64,
        hypotheses=ten_chinese_drafts(),
        provider_call_id="llmcall_" + "b" * 24,
    )
    assert tuple(item.logical_slot_id for item in batch.hypotheses) == tuple(
        f"H{i:02d}" for i in range(1, 11)
    )
```

同时断言缺槽、重复内容、英文占位叙事和原始响应字段均被拒绝。

- [x] **步骤 2：编写批准/拒绝完整性测试**

```python
def test_review_freezes_all_decisions_and_keeps_rejections() -> None:
    review = freeze_lightweight_review(batch, ten_mixed_decisions())
    assert review.decision_count == 10
    assert review.approved_hypothesis_count == 7
    assert review.rejected_hypothesis_count == 3
    assert review.candidate_family_size == 21
```

断言九条决定不能冻结；决定必须绑定 `draft_sha256`、`context_sha256`、审批角色和带时区时间；冻结后不能修改。

- [x] **步骤 3：运行失败测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_lightweight_hypotheses -v
```

- [x] **步骤 4：实现轻量假设与审批合同**

复用 `parse_evolution_hypothesis_response()` 的十槽、中文和治理键校验，不修改旧 `ApprovedEvolutionHypothesisBatch` 的“十条全批准”语义。新的 `LightweightReviewBatch` 明确允许 `approved` 和 `rejected`，但要求 H01–H10 全覆盖。

`generate_lightweight_hypotheses()` 只保存通过 Schema 的草案、请求哈希、响应哈希、provider/model 身份和调用 ID，不把原始响应投影到 Dashboard。

- [x] **步骤 5：运行目标测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_lightweight_hypotheses tests.test_research_evolution \
  tests.test_research_evolution_schema tests.test_llm_provider -v
```

- [x] **步骤 6：提交**

```bash
git add src/factor_miner/lightweight_hypotheses.py \
  src/factor_miner/research_evolution.py tests/test_lightweight_hypotheses.py
git commit -m "feat: freeze dashboard hypothesis reviews"
```

---

### 任务 3：一次请求批量生成批准假设的三个 AST

**文件：**

- 新建：`src/factor_miner/lightweight_expressions.py`
- 新建：`tests/test_lightweight_expressions.py`
- 修改：`src/factor_miner/lightweight_schema.py`

**接口：**

- 产出：`LightweightExpressionRequest`、`LightweightExpressionDesign`、`LightweightExpressionBatch`。
- 产出：`build_lightweight_expression_request(review, field_registry, ...) -> PreparedDeepSeekRequest`。
- 产出：`parse_lightweight_expression_response(response, review, registry) -> LightweightExpressionBatch`。
- 产出：`candidate_bindings_from_expression_batch(batch) -> tuple[LightweightCandidateBinding, ...]`。

- [x] **步骤 1：编写单次请求形状测试**

```python
def test_one_request_contains_only_approved_hypotheses() -> None:
    APPROVED = (1, 2, 4, 5, 7, 8, 10)
    request = build_lightweight_expression_request(review_with_seven_approved(), registry())
    assert request.slot_ids == tuple(
        f"H{h:02d}:C{c:03d}" for h in APPROVED for c in range(1, 4)
    )
    assert len(request.slot_ids) == 21
```

断言请求不含拒绝假设、研究结果、真实字段 ID、路径或密钥。

- [x] **步骤 2：编写局部失败不补抽测试**

```python
def test_invalid_design_keeps_original_slot_failed() -> None:
    batch = parse_lightweight_expression_response(
        response_with_one_invalid_ast(), review_with_two_approved(), registry()
    )
    assert batch.family_size == 6
    assert batch.failed_slot_ids == ("H02:C003",)
    assert len(batch.slot_results) == 6
```

同时断言每条批准假设恰好三个预留槽，不能只改变窗口冒充结构差异；本地字段、时点、lookback、白名单和 future probe 全部执行。

- [x] **步骤 3：运行失败测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_lightweight_expressions -v
```

- [x] **步骤 4：实现批量请求、解析和失败子集修复输入**

输出根对象固定为：

```json
{
  "hypotheses": [
    {
      "logical_slot_id": "H01",
      "designs": [
        {"candidate_slot_id": "H01:C001", "expression": {}},
        {"candidate_slot_id": "H01:C002", "expression": {}},
        {"candidate_slot_id": "H01:C003", "expression": {}}
      ]
    }
  ]
}
```

格式修复函数 `build_expression_repair_request()` 只接受 `failed_slot_ids`，并验证它们是原请求槽位的真子集；修复后仍失败则保留失败终态。

- [x] **步骤 5：运行目标测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_lightweight_expressions tests.test_compiler \
  tests.test_field_registry tests.test_lookahead tests.test_design_diversity -v
```

- [x] **步骤 6：提交**

```bash
git add src/factor_miner/lightweight_expressions.py \
  src/factor_miner/lightweight_schema.py tests/test_lightweight_expressions.py
git commit -m "feat: batch lightweight expression generation"
```

---

### 任务 4：实现可恢复单机 Worker 状态机

**文件：**

- 新建：`src/factor_miner/research_worker.py`
- 新建：`tests/test_research_worker.py`
- 修改：`src/factor_miner/lightweight_runner.py`

**接口：**

- 产出：`ResearchWorkerConfig`、`ResearchWorkerDependencies`、`ResearchWorker`。
- 产出：`ResearchWorker.process_once() -> WorkerTickResult`。
- 消费：任务 1 的命令 Store、任务 2 的假设和审批、任务 3 的表达式批次、现有轻量评价结果和发布投影端口。

- [x] **步骤 1：编写启动至等待审批测试**

```python
def test_start_calls_hypothesis_provider_once_then_waits() -> None:
    worker = ResearchWorker(config, dependencies=fakes())
    store.submit(ResearchCommand.start(requested_by="research_owner", requested_at=NOW))
    worker.process_once()
    state = store.active_state()
    assert state.stage is AutonomousStage.AWAITING_REVIEW
    assert dependencies.hypothesis_provider.call_count == 1
    worker.process_once()
    assert dependencies.hypothesis_provider.call_count == 1
```

- [x] **步骤 2：编写审批完成后的端到端恢复测试**

断言：

- 十条决定未齐时不调用表达式 provider；
- 全拒绝直接进入 `no_approved_hypothesis`；
- 七条批准只冻结二十一个槽；
- 正常路径表达式 provider 只调用一次；
- 发布后投影失败只重试投影；
- Worker 重启不重复模型调用、评价和发布；
- 单个槽失败不影响其他槽完成。

- [x] **步骤 3：运行失败测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_research_worker tests.test_lightweight_runner -v
```

- [x] **步骤 4：实现窄 Worker 编排**

`ResearchWorkerDependencies` 只定义阶段端口：

```python
class ResearchWorkerDependencies(Protocol):
    def prepare_context(self, state: AutonomousResearchState) -> PreparedContext: ...
    def generate_hypotheses(self, context: PreparedContext) -> LightweightHypothesisBatch: ...
    def generate_expressions(self, review: LightweightReviewBatch) -> LightweightExpressionBatch: ...
    def freeze_manifest(self, expressions: LightweightExpressionBatch) -> LightweightBatchManifest: ...
    def evaluate(self, manifest: LightweightBatchManifest) -> LightweightCampaignEvaluationResult: ...
    def publish(self, manifest, evaluation) -> PublishedLightweightRun: ...
    def project_control_state(self, state: AutonomousResearchState) -> None: ...
    def project(self, publication: PublishedLightweightRun) -> DashboardSnapshot: ...
    def refresh_evolution(self, publication: PublishedLightweightRun) -> EvolutionRefreshResult: ...
```

Worker 每次只推进一个阶段并写入新的不可变状态快照。每次推进前必须确认当前状态已投影到 PostgreSQL；状态文件已写但控制投影失败时，下一个 tick 只重试控制投影，不进入后续阶段。`AWAITING_REVIEW` 没有完整审批命令时返回空闲，不轮询 DeepSeek。

- [x] **步骤 5：运行目标测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_research_worker tests.test_lightweight_runner \
  tests.test_lightweight_schema tests.test_research_campaign_120_slots -v
```

- [x] **步骤 6：提交**

```bash
git add src/factor_miner/research_worker.py \
  src/factor_miner/lightweight_runner.py tests/test_research_worker.py
git commit -m "feat: add resumable single research worker"
```

---

### 任务 5：接入轻量记忆与因子图谱刷新

**文件：**

- 新建：`src/factor_miner/lightweight_evolution.py`
- 新建：`tests/test_lightweight_evolution.py`
- 修改：`src/factor_miner/research_worker.py`

**接口：**

- 产出：`LightweightMemoryEntry`、`LightweightMemorySnapshot`、`EvolutionRefreshResult`。
- 产出：`refresh_lightweight_evolution(publication, source_context, dependencies) -> EvolutionRefreshResult`。
- 产出：`build_lightweight_coverage_catalog(publication) -> LightweightCoverageCatalog`。
- 不修改旧 `ResearchMemoryEntry`、120 条批次和 generation seal 校验。

- [x] **步骤 1：编写全部假设终态入记忆测试**

```python
def test_rejected_failed_and_evaluated_items_enter_next_memory() -> None:
    result = refresh_lightweight_evolution(publication(), source_context(), fakes())
    assert result.hypothesis_entry_count == 10
    assert result.candidate_entry_count == publication().manifest.family_size
    assert result.source_memory_snapshot_id == source_context().memory_snapshot_id
    assert result.next_memory_snapshot_id != result.source_memory_snapshot_id
```

断言分钟字段候选具有 `intraday_aggregate` 标签；当前 manifest 仍绑定旧 memory/graph hash；同批 prompt 不含新结果。

- [x] **步骤 2：编写三产物齐全后才完成测试**

只有新记忆快照、新覆盖图谱和中文 gap brief 三者存在且哈希匹配时，`EvolutionRefreshResult` 才能构造；任一失败时 Worker 保持 `published` 或 `projected` 阶段并可重试。

- [x] **步骤 3：运行失败测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_lightweight_evolution -v
```

- [x] **步骤 4：实现轻量适配层**

轻量记忆写入独立的 `research_memory/lightweight/` 内容寻址对象和追加事件。`build_lightweight_coverage_catalog()` 把候选的规范 AST、字段签名、分钟来源标签和每日 IC 引用转换为覆盖图谱 builder 的显式输入；`refresh_lightweight_evolution()` 调用依赖端口 `publish_coverage_graph(catalog)` 生成新图谱，不用 glob 猜测输入。不得取消旧账本的 120 条断言。

- [x] **步骤 5：运行目标测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_lightweight_evolution tests.test_research_memory \
  tests.test_coverage_synthetic_e2e tests.test_research_worker -v
```

- [x] **步骤 6：提交**

```bash
git add src/factor_miner/lightweight_evolution.py \
  src/factor_miner/research_worker.py tests/test_lightweight_evolution.py
git commit -m "feat: refresh lightweight memory and factor graph"
```

---

### 任务 6：实现深色 Dashboard 运行台和 PostgreSQL 中文读模型

**文件：**

- 新建：`dashboard/research_control.py`
- 新建：`dashboard/pages/7_研究运行台.py`
- 新建：`dashboard/migrations/009_autonomous_research_control.sql`
- 新建：`tests/test_dashboard_research_control.py`
- 修改：`dashboard/app.py`
- 修改：`dashboard/pg_store.py`
- 修改：`dashboard/ui.py`

**接口：**

- 产出：`submit_start_command()`、`submit_review_decision()`、`submit_freeze_command()`、`submit_resume_command()`。
- 产出：PostgreSQL 表/视图 `research_runs`、`research_hypotheses`、`latest_research_run_zh`。
- Dashboard 页面只读 PostgreSQL 状态和通过 Schema 的假设全文；控制动作只写任务 1 的命令收件箱。

- [x] **步骤 1：编写页面合同测试**

```python
def test_hypothesis_detail_contains_every_review_field() -> None:
    detail = hypothesis_detail_payload(full_hypothesis())
    assert set(detail) == {
        "主张", "机制", "预期方向", "可观察代理", "独立验证",
        "竞争解释", "失效方式", "证伪路径", "来源与边界",
    }
```

断言摘要 payload 不包含审批动作；只有完整详情视图允许提交批准/拒绝；页面模型不包含 `token`、`usage`、原始响应、密钥或服务器私有路径。

- [x] **步骤 2：编写命令和 PostgreSQL 投影测试**

使用 fake connection 断言描述字段全部是中文，技术列为英文；重复按钮生成同一命令 ID；不完整指标不投影为零；`huanNNN` 分配逻辑继续复用 migration 007。

- [x] **步骤 3：运行失败测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_dashboard_research_control tests.test_dashboard_contract \
  tests.test_pg_store -v
```

- [x] **步骤 4：实现主从审批布局**

页面固定三栏：左侧 H01–H10 目录和状态，中间完整可滚动正文，右侧审批进度、信号时点和决策按钮。顶部显示 Worker 在线、批次、中文阶段和最近错误；运行失败时显示“从当前阶段继续”。不显示 TOKEN 数。

视觉使用现有 `dashboard/ui.py` 的主题入口，新增深蓝黑、青绿成功、低饱和红色拒绝和蓝色信息组件，避免把长 CSS 继续塞入页面文件。

- [x] **步骤 5：运行目标测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_dashboard_research_control tests.test_dashboard_contract \
  tests.test_dashboard_ui tests.test_pg_store -v
```

- [x] **步骤 6：提交**

```bash
git add dashboard/research_control.py dashboard/pages/7_研究运行台.py \
  dashboard/migrations/009_autonomous_research_control.sql \
  dashboard/app.py dashboard/pg_store.py dashboard/ui.py \
  tests/test_dashboard_research_control.py
git commit -m "feat: add autonomous research dashboard"
```

---

### 任务 7：接入真实服务器依赖、CLI 和 systemd

**文件：**

- 新建：`src/factor_miner/server_research_dependencies.py`
- 新建：`deploy/factor-miner-worker.service`
- 新建：`deploy/factor-miner-worker.env.example`
- 新建：`tests/test_server_research_dependencies.py`
- 修改：`src/factor_miner/cli.py`
- 修改：`tests/test_cli.py`

**接口：**

- CLI：`factor-miner research worker --config PATH --once`。
- CLI：`factor-miner research status --artifact-root PATH`。
- 常驻模式不带 `--once`，按冻结轮询间隔运行；`--once` 只处理一个 tick，供测试和部署诊断。
- `ServerResearchDependencies` 只从服务器显式配置加载数据端口、字段注册、模型政策、范围授权、评价配置、PostgreSQL DSN 和产物根。

- [x] **步骤 1：编写 CLI 和 Linux 边界测试**

```python
def test_research_help_lists_worker_and_status() -> None:
    result = runner.invoke(app, ["research", "--help"])
    assert result.exit_code == 0
    assert "worker" in result.stdout
    assert "status" in result.stdout

def test_real_worker_rejects_darwin() -> None:
    with patch("platform.system", return_value="Darwin"):
        assert runner.invoke(app, ["research", "worker", "--config", str(CONFIG), "--once"]).exit_code != 0
```

- [x] **步骤 2：编写真实依赖缺失硬失败测试**

断言缺少冻结分钟目录、字段注册表、评价 policy、scope authorization、`DEEPSEEK_API_KEY` 或 `FM_DASHBOARD_DSN` 时启动失败；不得静默切换录制 fixture、旧目录或 Mac 数据。

- [x] **步骤 3：运行失败测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_server_research_dependencies tests.test_cli -v
```

- [x] **步骤 4：实现服务器依赖装配和服务文件**

`server_research_dependencies.py` 只装配现有领域端口，不复制 compiler、evaluator 或发布逻辑。每个可用槽调用现有阶段 A Pilot runner；再把 Pilot 输出转换为 `CampaignSlotEvaluation` 并用 `LightweightCampaignEvaluationResult.build()` 汇总动态统计分母。发布复用 `publish_run_artifacts()` 和完整 Dashboard 指标闸门，投影复用 `PostgresDashboardStore`；任何核心指标缺失都拒绝发布。启动命令绑定 `ResearchStartAuthorization`，服务器预配置政策只允许固定 DeepSeek model、`hypothesis/expression` 两种角色和脱敏 payload，Dashboard 不再要求第二次外发授权操作。systemd 服务固定：

```ini
[Service]
Type=simple
WorkingDirectory=/home/dell/work/factor_miner_current
EnvironmentFile=/data/factor_miner_artifacts/config/worker.env
ExecStart=/home/dell/envs/work/bin/python -m factor_miner.cli research worker --config /data/factor_miner_artifacts/config/research-worker.json
Restart=on-failure
RestartSec=5
```

示例环境文件只列变量名，不包含真实 DSN、密钥或服务器运行配置值。

- [x] **步骤 5：运行目标测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_server_research_dependencies tests.test_cli \
  tests.test_research_worker tests.test_llm_server_runner -v
```

- [x] **步骤 6：提交**

```bash
git add src/factor_miner/server_research_dependencies.py src/factor_miner/cli.py \
  deploy/factor-miner-worker.service deploy/factor-miner-worker.env.example \
  tests/test_server_research_dependencies.py tests/test_cli.py
git commit -m "feat: add autonomous research worker service"
```

---

### 任务 8：完整回归、录制验收和服务器部署

**文件：**

- 修改：`docs/superpowers/plans/2026-08-14-dashboard-worker-autonomous-research.md`
- 新建：`docs/runbooks/Dashboard自主研究运行.md`

**接口：**

- 产出：用户只需“新建批次 → 审批十条 → 冻结并运行 → 查看完成状态”的中文运行手册。
- 不自动触发首次真实 DeepSeek 调用；部署只使用录制响应验收。

- [x] **步骤 1：运行本地全量回归**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest discover -s tests -v
```

预期：全部通过；仅允许既有、已解释的平台跳过。

- [x] **步骤 2：创建独立服务器部署目录并同步提交**

使用 `git archive` 部署到 `/home/dell/work/factor_miner_deploy_<commit>`，不得覆盖服务器现有源码目录或未提交修改。创建或更新 `/home/dell/work/factor_miner_current` 符号链接前先核验目标目录和提交身份。

- [x] **步骤 3：运行服务器全量测试和录制 Worker 验收**

```bash
PYTHONPATH=src /home/dell/envs/work/bin/python -m unittest discover -s tests -q
PYTHONPATH=src /home/dell/envs/work/bin/python -m factor_miner.cli research worker \
  --config /data/factor_miner_artifacts/config/research-worker-recorded.json --once
```

录制验收必须证明：十条假设可见、混合审批可冻结、表达式调用计数为一、动态分母正确、发布投影可恢复、记忆和图谱只供下一批使用。

- [x] **步骤 4：执行 PostgreSQL migration 009 并核验中文读模型**

核验 `latest_research_run_zh`、十条假设全文、审批状态和中文阶段；确认 Dashboard 查询不返回 TOKEN、原始响应或 NULL 核心指标候选。

- [x] **步骤 5：切换 Dashboard 并启动 Worker service**

先启动 Worker 并检查 `systemctl --user status factor-miner-worker.service`，再重启 Streamlit。核验：

- Worker 状态在线；
- Dashboard 健康端点返回 `ok`；
- 页面 cwd 和 Worker cwd 都指向同一部署提交；
- 页面能提交幂等启动命令但部署验收不实际发送 DeepSeek 请求；
- 服务重启后录制批次从原阶段继续。

- [x] **步骤 6：编写中文运行手册并提交**

运行手册只写 Dashboard 操作、阶段含义、恢复按钮和研究边界，不记录密码、DSN、密钥或真实运行路径解析值。

```bash
git add docs/runbooks/Dashboard自主研究运行.md \
  docs/superpowers/plans/2026-08-14-dashboard-worker-autonomous-research.md
git commit -m "docs: document autonomous dashboard research"
```

---

## 完成定义

- Dashboard 可创建一个活动批次并完整展示十条不截断假设。
- 用户必须逐条批准或拒绝，十条决策未齐时不能继续。
- 正常批次只调用一次假设模型和一次批量表达式模型。
- 候选检验族在读取结果前冻结为批准假设数乘三，失败槽位不消失。
- Worker、Dashboard 或浏览器重启不会重复调用模型或破坏已发布产物。
- 评价候选只有完整核心指标才进入 PostgreSQL，名称稳定为 `huanNNN`，描述为中文。
- 本批全部假设和候选终态进入下一轮记忆，因子图谱保留并更新，同批不自我反馈。
- 公司 Linux 全量测试、录制端到端验收、PostgreSQL migration、Dashboard 健康检查和 Worker service 检查全部通过。
- 第一次真实 DeepSeek 研究仍由用户在 Dashboard 主动点击触发。
