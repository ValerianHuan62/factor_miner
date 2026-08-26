# Factor Miner 轻量进化研究实施计划

> **面向执行代理：** 必须使用 `superpowers:executing-plans` 按任务顺序实施；每个任务都以独立测试和独立提交结束。

**目标：** 把现有 120 槽多阶段研究系统收敛为默认 30 槽的单入口流程，同时保留研究记忆、因子图谱、可信评价和服务器分钟聚合能力。

**架构：** 复用现有候选 Schema、白名单 AST、IC/HAC、组合评价、记忆和覆盖图谱算法，只新增一个轻量应用服务负责编排。PostgreSQL 通过中文目录视图和“最新完整评价”视图解决旧运行 NULL 混入问题；分钟 HDF5 使用独立只读适配器聚合为日级截面字段，不改变日频评价器。

**技术栈：** Python 3.12、Pydantic 2、Polars、Typer、PostgreSQL 16、Streamlit、h5py/HDF5、规范 JSON/JSONL。

## 全局约束

- 默认批次为 10 个假设、每个假设 3 个候选，共 30 槽；允许运行前显式冻结其他正整数槽数。
- 当前批结果不能改变当前批候选、方向、标签、股票池、成本、HAC 参数或检验族规模。
- 分钟数据唯一正式源是 `/data/quantlake/raw/market/minbar_h5/equities_final_2005_20260630`。
- 第一版分钟信号只使用完整交易日结束后的数据，最早在下一交易日开盘使用。
- PostgreSQL 字段名和技术标识保留英文；所有面向人的名称、描述、状态和标签使用中文。
- 旧半空评价不得填零或删除，只能从默认完整评价视图排除。
- 研究记忆和因子图谱只影响下一批，不得在同一批内根据结果反向调参。
- Mac 只运行合成测试；真实日频、分钟计算和服务器 PostgreSQL 验收只在公司 Linux 执行。

---

### 任务 1：建立中文候选目录和最新完整评价视图

**文件：**

- 新建：`dashboard/migrations/008_complete_chinese_candidate_views.sql`
- 新建：`src/factor_miner/dashboard_labels.py`
- 修改：`dashboard/ui.py`
- 修改：`dashboard/pages/1_批次总览.py`
- 修改：`tests/test_dashboard_contract.py`
- 修改：`tests/test_dashboard_projection.py`

**接口：**

- 产出：`chinese_candidate_label(field: str, value: object) -> str`
- 产出：PostgreSQL 视图 `candidate_catalog_zh`
- 产出：PostgreSQL 视图 `latest_complete_candidate_metrics`
- 完整评价条件：13 个核心指标全部为非 NULL。

- [x] **步骤 1：编写中文标签失败测试**

```python
def test_dashboard_labels_translate_descriptive_values() -> None:
    assert chinese_candidate_label("expected_sign", "positive") == "正向"
    assert chinese_candidate_label("availability", "next_open") == "当日收盘观察，下一交易日开盘使用"
    assert chinese_candidate_label("mechanism_status", "mechanism_unverified") == "机制尚未独立验证"
    assert chinese_candidate_label("quality_status", "not_assessed") == "待评价"
    assert chinese_candidate_label("source_kind", "deepseek") == "LLM"
```

- [x] **步骤 2：编写 SQL 视图结构失败测试**

```python
def test_complete_view_requires_every_core_metric() -> None:
    sql = Path("dashboard/migrations/008_complete_chinese_candidate_views.sql").read_text()
    for field in REQUIRED_CORE_METRICS:
        assert f"{field} IS NOT NULL" in sql
    assert "row_number() OVER" in sql
    assert "PARTITION BY candidate_id" in sql
```

- [x] **步骤 3：运行测试并确认失败**

运行：

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_dashboard_contract tests.test_dashboard_projection -v
```

预期：因 `dashboard_labels` 和迁移 008 不存在而失败。

- [x] **步骤 4：实现中文映射和 PostgreSQL 视图**

`dashboard_labels.py` 使用确定性映射；未知值保留原技术值，不猜测含义：

```python
_LABELS = {
    ("expected_sign", "positive"): "正向",
    ("expected_sign", "negative"): "负向",
    ("availability", "next_open"): "当日收盘观察，下一交易日开盘使用",
    ("mechanism_status", "mechanism_unverified"): "机制尚未独立验证",
    ("quality_status", "not_assessed"): "待评价",
    ("source_kind", "human"): "人工",
    ("source_kind", "deepseek"): "LLM",
}
```

迁移 008 创建 `candidate_catalog_zh`，以 `CASE` 输出中文描述列；创建 `latest_complete_candidate_metrics`，先过滤核心指标完整的行，再按 `candidate_id` 和 `research_runs.projected_at DESC` 取一行。保留 `run_id` 供审计追溯。

- [x] **步骤 5：让 Dashboard 总览显示 `huanNNN` 和中文状态**

页面中的“内部编号”改为“因子编号”，`candidate_name()` 对 `huanNNN` 直接返回编号；预期方向、来源和质量状态统一调用 `chinese_candidate_label()`。

- [x] **步骤 6：运行目标回归**

运行：

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_dashboard_contract tests.test_dashboard_projection tests.test_pilot_projection -v
```

预期：全部通过。

- [x] **步骤 7：提交**

```bash
git add dashboard/migrations/008_complete_chinese_candidate_views.sql \
  src/factor_miner/dashboard_labels.py dashboard/ui.py \
  dashboard/pages/1_批次总览.py tests/test_dashboard_contract.py \
  tests/test_dashboard_projection.py
git commit -m "feat: add complete Chinese dashboard views"
```

---

### 任务 2：冻结轻量批次配置和 30 槽合同

**文件：**

- 新建：`src/factor_miner/lightweight_schema.py`
- 新建：`tests/test_lightweight_schema.py`
- 修改：`src/factor_miner/research_campaign_runner.py`
- 修改：`tests/test_research_campaign_120_slots.py`

**接口：**

- 产出：`LightweightResearchConfig`
- 产出：`LightweightBatchManifest`
- 产出：`build_lightweight_batch_manifest(config, hypotheses, candidates)`
- 消费：现有规范 Candidate Spec、评价政策哈希、记忆快照 ID 和覆盖图谱 ID。

- [x] **步骤 1：编写默认 30 槽失败测试**

```python
def test_default_lightweight_batch_has_thirty_frozen_slots() -> None:
    config = LightweightResearchConfig()
    assert config.hypothesis_count == 10
    assert config.candidates_per_hypothesis == 3
    assert config.slot_count == 30
```

- [x] **步骤 2：编写冻结身份和自定义槽数失败测试**

```python
def test_manifest_identity_changes_when_candidate_or_policy_changes() -> None:
    first = build_manifest(candidate_hash="a" * 64, family_size=30)
    second = build_manifest(candidate_hash="b" * 64, family_size=30)
    assert first.manifest_sha256 != second.manifest_sha256
    assert first.family_size == 30
```

同时断言假设数、每假设候选数必须为正，实际候选数量必须与冻结配置完全一致。

- [x] **步骤 3：运行失败测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_lightweight_schema -v
```

预期：模块不存在而失败。

- [x] **步骤 4：实现不可变轻量配置与 manifest**

```python
class LightweightResearchConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    hypothesis_count: int = Field(default=10, gt=0)
    candidates_per_hypothesis: int = Field(default=3, gt=0)
    allow_external_llm: bool = True

    @property
    def slot_count(self) -> int:
        return self.hypothesis_count * self.candidates_per_hypothesis
```

`LightweightBatchManifest` 绑定有序候选 hash、评价政策 hash、记忆快照 hash、覆盖图谱 hash 和 `family_size`。不复用旧的强制 120 槽 Schema。

- [x] **步骤 5：把正式评价器的 family size 改为读取 manifest**

只替换研究族规模来源，不改变 HAC、Bonferroni、IC 或组合计算。旧 120 槽运行继续由旧类型解析。

- [x] **步骤 6：运行目标测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_lightweight_schema tests.test_research_campaign_120_slots \
  tests.test_statistics -v
```

预期：全部通过，旧 120 槽兼容测试不退化。

- [x] **步骤 7：提交**

```bash
git add src/factor_miner/lightweight_schema.py \
  src/factor_miner/research_campaign_runner.py \
  tests/test_lightweight_schema.py tests/test_research_campaign_120_slots.py
git commit -m "feat: freeze lightweight research batches"
```

---

### 任务 3：实现单命令轻量研究编排与恢复

**文件：**

- 新建：`src/factor_miner/lightweight_runner.py`
- 新建：`tests/test_lightweight_runner.py`
- 修改：`src/factor_miner/cli.py`
- 修改：`tests/test_cli.py`

**接口：**

- 消费：`LightweightResearchConfig`、现有 LLM provider、候选校验器、campaign evaluator、发布器、PostgreSQL store、记忆 store 和覆盖图谱 builder。
- 产出：`run_lightweight_research(config_path: Path) -> LightweightRunSummary`
- CLI：`factor-miner research run --config PATH`

- [x] **步骤 1：编写单入口与恢复失败测试**

```python
def test_replay_resumes_without_recalling_completed_generation() -> None:
    first = run_lightweight_research(config_path, dependencies=fakes())
    second = run_lightweight_research(config_path, dependencies=fakes())
    assert second.run_id == first.run_id
    assert provider.call_count == 1
    assert evaluator.call_count == 1
```

测试还应断言：评价前 manifest 已落盘；投影失败不会破坏已发布产物；单个候选失败不会自动补抽。

- [x] **步骤 2：运行失败测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_lightweight_runner tests.test_cli -v
```

预期：轻量 runner 和 `research run` 命令不存在。

- [x] **步骤 3：实现窄应用服务**

runner 只编排阶段，不重新实现领域算法：

```python
class LightweightStage(str, Enum):
    CONTEXT_READY = "context_ready"
    BATCH_FROZEN = "batch_frozen"
    EVALUATED = "evaluated"
    PUBLISHED = "published"
    PROJECTED = "projected"
    EVOLUTION_REFRESHED = "evolution_refreshed"
```

状态文件只保存 `run_id`、stage、manifest hash 和产物引用。恢复时核对已有文件身份后从下一阶段继续，不建立第二套复杂 slot 状态机。

- [ ] **步骤 4：接入 Typer 命令**

新增 `research_app = typer.Typer()`，根命令注册 `research`。配置文件只含服务器本地路径、默认 30 槽参数和是否调用 LLM；数据库 DSN 继续从 `FM_DASHBOARD_DSN` 读取。

- [ ] **步骤 5：运行目标测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_lightweight_runner tests.test_cli tests.test_llm_orchestrator \
  tests.test_research_campaign_evaluation -v
```

预期：全部通过。

- [ ] **步骤 6：提交**

```bash
git add src/factor_miner/lightweight_runner.py src/factor_miner/cli.py \
  tests/test_lightweight_runner.py tests/test_cli.py
git commit -m "feat: add one-command lightweight research"
```

---

### 任务 4：把研究记忆和因子图谱接回下一轮

**文件：**

- 新建：`src/factor_miner/evolution_refresh.py`
- 新建：`tests/test_evolution_refresh.py`
- 修改：`src/factor_miner/research_memory.py`
- 修改：`src/factor_miner/coverage_workflow.py`
- 修改：`src/factor_miner/lightweight_runner.py`

**接口：**

- 产出：`refresh_next_round_context(publication, previous_memory, previous_graph) -> EvolutionRefreshResult`
- 结果包含：新记忆快照 ID、新覆盖图谱 ID、下一轮中文 gap 简报 hash。

- [ ] **步骤 1：编写跨批隔离失败测试**

```python
def test_current_batch_does_not_read_its_new_memory() -> None:
    result = refresh_next_round_context(current_publication, old_memory, old_graph)
    assert current_manifest.memory_snapshot_id == old_memory.snapshot_id
    assert result.next_memory_snapshot_id != old_memory.snapshot_id
```

测试还应断言失败、重复和数据不可用槽都进入记忆；分钟结构标签进入图谱；同一批 prompt 不包含当前批结果。

- [ ] **步骤 2：运行失败测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_evolution_refresh -v
```

- [ ] **步骤 3：实现下一轮刷新服务**

服务顺序固定为：发布完成 → 追加全部槽位记忆 → 构建记忆快照 → 更新覆盖目录 → 构建图谱 → 生成下一轮中文缺口简报。任何一步失败不回滚已发布研究结果，但保持当前运行状态为 `published`，重跑可继续刷新。

- [ ] **步骤 4：接入轻量 runner 最终阶段**

`EVOLUTION_REFRESHED` 只在新记忆快照、图谱和 gap brief 三者全部存在且 hash 可核验后写入。

- [ ] **步骤 5：运行目标测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_evolution_refresh tests.test_research_memory \
  tests.test_coverage_synthetic_e2e tests.test_lightweight_runner -v
```

- [ ] **步骤 6：提交**

```bash
git add src/factor_miner/evolution_refresh.py \
  src/factor_miner/research_memory.py src/factor_miner/coverage_workflow.py \
  src/factor_miner/lightweight_runner.py tests/test_evolution_refresh.py
git commit -m "feat: evolve memory and factor graph between batches"
```

---

### 任务 5：接入唯一正式分钟 HDF5 并聚合为日级信号

**文件：**

- 新建：`src/factor_miner/intraday_schema.py`
- 新建：`src/factor_miner/intraday_data.py`
- 新建：`src/factor_miner/intraday_features.py`
- 新建：`tests/test_intraday_schema.py`
- 新建：`tests/test_intraday_features.py`
- 修改：`pyproject.toml`
- 修改：`uv.lock`
- 修改：`src/factor_miner/field_registry.py`

**接口：**

- 产出：`IntradaySourceSpec`
- 产出：`inspect_hdf5_contract(path: Path) -> IntradayFileContract`
- 产出：`aggregate_intraday_day(frame: pl.DataFrame, spec: IntradaySourceSpec) -> pl.DataFrame`
- 输出主键：`date, asset`；字段前缀：`intraday_`。

- [x] **步骤 1：编写唯一目录和未来时点失败测试**

```python
def test_intraday_source_accepts_only_frozen_directory() -> None:
    spec = IntradaySourceSpec(root=FROZEN_MINUTE_ROOT)
    assert spec.root == Path("/data/quantlake/raw/market/minbar_h5/equities_final_2005_20260630")
    with pytest.raises(ValueError):
        IntradaySourceSpec(root=Path("/data/quantlake/raw/market/minbar_h5/equities"))
```

同时断言 earliest use 为下一交易日开盘，不能配置盘中成交。

- [x] **步骤 2：编写合成分钟聚合失败测试**

构造包含上午、午休、下午、尾盘、重复时间戳和缺分钟日的纯合成面板，断言：

```python
assert row["intraday_open_30m_return"] == expected_open_return
assert row["intraday_close_30m_amount_share"] == expected_tail_share
assert row["intraday_realized_volatility"] == expected_rv
assert row["intraday_close_vwap_deviation"] == expected_vwap_deviation
assert incomplete["completeness_status"] == "分钟数据不完整"
```

- [x] **步骤 3：运行失败测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_intraday_schema tests.test_intraday_features -v
```

- [x] **步骤 4：记录并锁定 HDF5 依赖**

服务器只读探针已经确认正式文件是根节点 `data` 下的 HDF5 compound dataset，不是必须依赖 pandas/PyTables 的 HDFStore。直接使用 `h5py`：官方仓库 `h5py/h5py` 在 2026-08-13 约 2.2k star，最近 release 为 3.16.0（2026-03-06），PyPI 标记为 Production/Stable、支持 Python 3.12，BSD-3-Clause。将依赖固定为 `h5py>=3.16,<3.17` 并更新 `uv.lock`。

- [x] **步骤 5：实现 HDF5 合同探针和日级聚合**

合同探针只返回 key、列名、dtype、首尾时间、行数和内容 hash，不打印个股数据。聚合器显式处理 09:30–11:30、13:00–15:00、午休、重复时间戳、零成交和缺分钟；不完整日保留 NULL，不使用 `fillna(0)`。

- [x] **步骤 6：把分钟聚合字段注册为日级可用字段**

字段的 `panel_shape` 仍为 `asset_date_scalar`，`event_time=close_t`，`earliest_decision_time=after_close_t`；LLM 只看到公开别名和中文含义，不看到原始 HDF5 内容。

- [x] **步骤 7：运行目标测试**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest \
  tests.test_intraday_schema tests.test_intraday_features \
  tests.test_field_registry tests.test_lookahead tests.test_compiler -v
```

- [x] **步骤 8：提交**

```bash
git add src/factor_miner/intraday_schema.py src/factor_miner/intraday_data.py \
  src/factor_miner/intraday_features.py src/factor_miner/field_registry.py \
  tests/test_intraday_schema.py tests/test_intraday_features.py \
  pyproject.toml uv.lock
git commit -m "feat: aggregate frozen intraday data into daily factors"
```

---

### 任务 6：完整回归和公司 Linux 部署

**文件：**

- 新建：`configs/lightweight_research.env.example`
- 新建：`docs/runbooks/轻量进化研究服务器运行.md`
- 修改：`README.md`
- 修改：`docs/START_HERE.md`

**接口：**

- 部署输入：冻结 Git 提交、`uv.lock`、服务器私有配置和现有 PostgreSQL DSN。
- 部署输出：运行 ID、完整指标计数、记忆快照 ID、图谱 ID、分钟合同摘要和 Dashboard 健康状态。

- [ ] **步骤 1：运行本地全量回归**

```bash
UV_CACHE_DIR=/tmp/factor-miner-uv-cache uv run python -m unittest discover -s tests -v
```

预期：全部通过；Mac 不访问服务器真实数据。

- [ ] **步骤 2：编写中文服务器运行说明和示例配置**

说明只暴露变量名，不写 DSN、密码和真实私有路径内容。记录默认 30 槽、唯一分钟目录、恢复命令、完整评价定义和失败排查方法。

- [ ] **步骤 3：提交文档与配置**

```bash
git add configs/lightweight_research.env.example \
  docs/runbooks/轻量进化研究服务器运行.md README.md docs/START_HERE.md
git commit -m "docs: add lightweight research server runbook"
```

- [ ] **步骤 4：部署冻结提交到新目录**

在服务器创建 `/home/dell/work/factor_miner_deploy_<commit>`，不得覆盖当前带未提交修改的 `/home/dell/work/factor_miner`。运行：

```bash
uv sync --frozen --extra dashboard
uv run python -m unittest discover -s tests -v
```

- [ ] **步骤 5：执行 PostgreSQL 迁移和只读核验**

按编号执行迁移 008，然后查询：候选总数、`huanNNN` 数量、完整评价视图数量、旧不完整行数量和中文目录空值数量。禁止修改或删除旧研究运行。

- [ ] **步骤 6：探测正式分钟目录合同**

只读取一个明确文件的 HDF5 metadata，验证 key、列、时间戳、交易区间和截止日期；不把原始记录输出到日志或模型上下文。

- [ ] **步骤 7：切换 Dashboard 服务**

停止旧 `/home/dell/work/factor_miner_deploy_0263a17` Streamlit 进程，使用新部署目录和原有私有环境启动。验证只监听 `127.0.0.1:8501`，首页展示 `huanNNN`、中文状态且当前 120 个完整候选没有核心 NULL。

- [ ] **步骤 8：执行轻量批次 smoke**

先以 `allow_external_llm=false` 和程序生成的服务器合成候选运行 3 槽 smoke，确认冻结、评价、发布、投影、记忆和图谱刷新闭环；再由用户决定是否启动真实 30 槽 LLM 批次。

## 计划自检

- 设计中的 PostgreSQL 中文、完整评价、30 槽、单命令、记忆、图谱、分钟正式源和服务器部署均有对应任务。
- 所有新增函数和类型在首次使用的任务中定义，后续任务只消费前序接口。
- 旧 120 槽、旧半空运行和当前服务器未提交修改均明确保留。
- 没有把盘中交易、生产晋级或当前批结果反馈加入本轮范围。
