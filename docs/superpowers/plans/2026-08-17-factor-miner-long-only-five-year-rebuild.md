# 因子挖掘目标多头五年协议实施计划

> **供自动化开发代理使用：** 必须按任务逐项执行；推荐使用 `superpowers:subagent-driven-development`，也可在当前会话内按测试先行方式执行。所有步骤使用复选框跟踪。

**目标：** 将正式研究改为沪深动态全市场的目标多头评价，用 2021—2023 年发现并冻结方向、2024—2026 年确认、2025—2026 年展示近期表现，只对确认优秀的因子开放长历史压力测试，并重新计算现有 29 个因子后重建 PostgreSQL 读模型。

**架构：** 正式 JSON/JSONL/Parquet 继续是唯一研究账本；方向发现结果作为不可变评价身份写入候选结果与研究记忆，绝不覆盖事前假设方向。组合层只发布目标多头与基准；极端分组差只在内部计算 `win_rate`，不作为可交易组合、数据库指标或 Dashboard 展示。PostgreSQL 仅保存中文因子说明、方向关系和目标多头摘要。

**技术栈：** Python 3.12、Pydantic、Polars、Streamlit、PostgreSQL、systemd、JSON/JSONL 正式账本。

## 全局约束

- 股票池为每个信号日可得的沪深全市场，不允许使用期末股票列表回填历史。
- 方向发现区间固定为 2021-01-01 至 2023-12-31，确认区间固定为 2024-01-01 至 2026-06-30，近期展示区间固定为 2025-01-01 至 2026-06-30。
- 事前假设方向只用于判断“支持、反转或不明确”；目标多头方向由发现区间 RankIC 符号决定并在读取确认区间结果前冻结。
- 正式收益只发布目标多头等权组合；观察时点为前一交易日收盘，最早成交为下一可交易日开盘，成本按实际换手乘冻结的双边费率。
- `win_rate` 继续由内部冻结极端分组差逐期收益计算并作为非空治理指标写入 PostgreSQL，但不得作为组合收益投影或在 Dashboard 展示，不得称为多空策略。
- 2012 年以来长历史仅对确认阶段已通过的因子按显式命令执行，不得在普通批次自动运行。
- 所有描述性文字和值使用中文；字段、路径、标准缩写可使用英文。
- 真实行情计算、29 因子重跑和正式验证只在公司 Linux 服务器执行；Mac 只运行合成测试。
- 旧协议不做兼容：仅在删除前提取现有 29 份不可变公式/假设 Spec 与稳定 `huanNNN` 映射；新协议发布验收后删除全部旧批次、旧运行结果、旧审批状态、旧数据库投影和旧研究记忆。

---

### Task 1：冻结研究区间与方向发现合同

**文件：**
- 新建：`src/factor_miner/long_only_protocol.py`
- 修改：`src/factor_miner/pilot_schema.py`
- 修改：`src/factor_miner/pilot_sources.py`
- 测试：`tests/test_long_only_protocol.py`

**接口：**
- 产出：`LongOnlyResearchProtocol`、`DirectionDecision`、`select_direction(discovery_rank_ic)`。
- 消费：候选事前 `expected_sign`、发现区间 RankIC 摘要。

- [ ] **步骤 1：先写失败测试**

```python
def test_default_protocol_freezes_three_non_overlapping_windows() -> None:
    policy = LongOnlyResearchProtocol()
    assert policy.discovery_start == date(2021, 1, 1)
    assert policy.discovery_end == date(2023, 12, 31)
    assert policy.confirmation_start == date(2024, 1, 1)
    assert policy.confirmation_end == date(2026, 6, 30)
    assert policy.recent_start == date(2025, 1, 1)

def test_direction_is_selected_without_overwriting_hypothesis() -> None:
    decision = select_direction(-0.01, hypothesis_direction="positive")
    assert decision.selected_direction == "negative"
    assert decision.hypothesis_relation == "reversed"
```

- [ ] **步骤 2：运行测试并确认因模块缺失而失败**

运行：`.venv/bin/python -m unittest tests.test_long_only_protocol -v`

- [ ] **步骤 3：实现最小冻结合同**

```python
class LongOnlyResearchProtocol(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal["long-only-v1"] = "long-only-v1"
    discovery_start: date = date(2021, 1, 1)
    discovery_end: date = date(2023, 12, 31)
    confirmation_start: date = date(2024, 1, 1)
    confirmation_end: date = date(2026, 6, 30)
    recent_start: date = date(2025, 1, 1)
    recent_end: date = date(2026, 6, 30)
    universe: Literal["SSE_SZSE_WHOLE_MARKET"] = "SSE_SZSE_WHOLE_MARKET"
```

`pilot_sources.py` 必须按每个交易日的状态表与行情交集动态选股，并显式只接受 `.XSHG`、`.XSHE`；不得依赖 `universe_uri=None` 的隐式行为，也不得读取旧 CSI300 成分股开盘文件。

- [ ] **步骤 4：运行测试并确认通过**

运行：`.venv/bin/python -m unittest tests.test_long_only_protocol -v`

- [ ] **步骤 5：提交**

```bash
git add src/factor_miner/long_only_protocol.py src/factor_miner/pilot_schema.py src/factor_miner/pilot_sources.py tests/test_long_only_protocol.py
git commit -m "feat: freeze long-only research windows"
```

### Task 2：只发布目标多头组合

**文件：**
- 修改：`src/factor_miner/portfolio_schema.py`
- 修改：`src/factor_miner/portfolio_evaluation.py`
- 修改：`src/factor_miner/portfolio_statistics.py`
- 修改：`src/factor_miner/pilot_runner.py`
- 测试：`tests/test_portfolio_evaluation.py`
- 测试：`tests/test_portfolio_statistics.py`
- 测试：`tests/test_pilot_runner.py`

**接口：**
- 消费：任务 1 的 `DirectionDecision.selected_direction`。
- 产出：`target_long_gross_return`、`target_long_net_return`、`benchmark_return`，以及仅进入治理摘要和 PostgreSQL `win_rate` 列的内部极端分组诊断。

- [ ] **步骤 1：先把现有多空断言改为失败的目标多头断言**

```python
def test_only_target_long_and_benchmark_are_published() -> None:
    result = run_target_long_backtest(
        panel(), benchmark(), SCHEDULE, PortfolioEvaluationPolicy(),
        direction="negative",
    )
    first = result.daily_returns[0]
    assert "target_long_net_return" in first
    assert "Q10_Q1_net_return" not in first
    assert "Q9_Q2_net_return" not in first
```

- [ ] **步骤 2：运行测试并确认旧实现仍发布多空列而失败**

运行：`.venv/bin/python -m unittest tests.test_portfolio_evaluation tests.test_portfolio_statistics -v`

- [ ] **步骤 3：实现目标多头分组、换手、成本和指标**

```python
target = ordered.filter(
    pl.col("_group") == (1 if direction == "positive" else policy.group_count)
)
net_return = gross_return - turnover * policy.round_trip_cost_bps / 10_000.0
```

内部极端组差只生成 `win_rate` 所需逐期诊断并写入正式审计摘要；组合日序列和组合指标不得包含多空收益列。

- [ ] **步骤 4：验证正向选高值、负向选低值，回撤使用目标多头净值**

运行：`.venv/bin/python -m unittest tests.test_portfolio_evaluation tests.test_portfolio_statistics tests.test_pilot_runner -v`

- [ ] **步骤 5：提交**

```bash
git add src/factor_miner/portfolio_schema.py src/factor_miner/portfolio_evaluation.py src/factor_miner/portfolio_statistics.py src/factor_miner/pilot_runner.py tests/test_portfolio_evaluation.py tests/test_portfolio_statistics.py tests/test_pilot_runner.py
git commit -m "feat: publish target-long portfolio metrics"
```

### Task 3：执行方向发现、确认与近期评价

**文件：**
- 修改：`src/factor_miner/pilot_runner.py`
- 修改：`src/factor_miner/pilot_schema.py`
- 修改：`src/factor_miner/pilot_artifacts.py`
- 修改：`src/factor_miner/server_research_dependencies.py`
- 修改：`src/factor_miner/cli.py`
- 测试：`tests/test_pilot_runner.py`
- 测试：`tests/test_pilot_artifacts.py`
- 测试：`tests/test_server_research_dependencies.py`

**接口：**
- 消费：完整 2021-01-01 至 2026-06-30 因子面板与冻结标签。
- 产出：发现区间方向决定、确认区间正式指标、近期区间指标、压力测试资格状态。
- 产出：`pilot reevaluate-existing` 正式入口，从已发布运行的 29 个不可变 Spec 产生一个全新的运行，不调用 LLM、不覆盖旧产物。

- [ ] **步骤 1：写失败测试，证明确认结果不能参与方向选择**

```python
def test_direction_uses_discovery_only_and_confirmation_uses_frozen_direction() -> None:
    result = evaluate_long_only_windows(panel_with_opposite_period_signs())
    assert result.direction.discovery_rank_ic_mean > 0
    assert result.direction.selected_direction == "positive"
    assert result.confirmation.rank_ic_mean < 0
    assert result.direction.hypothesis_relation in {"supported", "reversed"}
```

- [ ] **步骤 2：运行聚焦测试并确认失败**

运行：`.venv/bin/python -m unittest tests.test_pilot_runner tests.test_pilot_artifacts -v`

- [ ] **步骤 3：实现一次计算、三段只读切片**

方向决定对象必须在构建确认标签统计和目标多头组合前生成；确认通过状态才设置 `stress_test_eligible=True`，普通 Worker 不执行长历史压力测试。

- [ ] **步骤 4：验证自主 Worker 使用同一冻结协议**

运行：`.venv/bin/python -m unittest tests.test_server_research_dependencies -v`

- [ ] **步骤 5：提交**

```bash
git add src/factor_miner/pilot_runner.py src/factor_miner/pilot_schema.py src/factor_miner/pilot_artifacts.py src/factor_miner/server_research_dependencies.py src/factor_miner/cli.py tests/test_pilot_runner.py tests/test_pilot_artifacts.py tests/test_server_research_dependencies.py tests/test_cli.py
git commit -m "feat: separate direction discovery and confirmation"
```

### Task 4：把方向支持或反转写入研究记忆

**文件：**
- 修改：`src/factor_miner/research_evolution_schema.py`
- 修改：`src/factor_miner/research_memory.py`
- 修改：`src/factor_miner/server_research_dependencies.py`
- 测试：`tests/test_research_memory_projection.py`
- 测试：`tests/test_research_evolution.py`

**接口：**
- 消费：任务 3 的不可变 `DirectionDecision`。
- 产出：记忆条目的 `direction_relation` 与中文失败说明；原假设方向保持不变。

- [ ] **步骤 1：写失败测试，禁止反向结果覆盖原假设**

```python
def test_reversed_direction_is_memory_not_hypothesis_mutation() -> None:
    entry = build_memory_entry(
        hypothesis_direction="negative",
        selected_direction="positive",
    )
    assert entry.direction_relation == "reversed"
    assert entry.hypothesis_direction == "negative"
    assert entry.selected_direction == "positive"
```

- [ ] **步骤 2：运行测试并确认 schema 缺字段而失败**

运行：`.venv/bin/python -m unittest tests.test_research_evolution tests.test_research_memory_projection -v`

- [ ] **步骤 3：实现离散方向记忆并纳入下一轮缺口上下文**

不得把逐期收益、个股或私有路径写入公开记忆；中文说明采用“事前负向假设在方向发现区间被反转，确认结果另行记录”。

- [ ] **步骤 4：运行记忆回归测试**

运行：`.venv/bin/python -m unittest tests.test_research_evolution tests.test_research_memory_projection -v`

- [ ] **步骤 5：提交**

```bash
git add src/factor_miner/research_evolution_schema.py src/factor_miner/research_memory.py src/factor_miner/server_research_dependencies.py tests/test_research_evolution.py tests/test_research_memory_projection.py
git commit -m "feat: remember discovered factor direction"
```

### Task 5：重建简洁数据库与目标多头 Dashboard

**文件：**
- 新建：`dashboard/migrations/012_rebuild_long_only_dashboard_schema.sql`
- 修改：`dashboard/pg_store.py`
- 修改：`dashboard/app.py`
- 修改：`dashboard/pages/1_批次总览.py`
- 修改：`dashboard/pages/3_分组回测.py`
- 修改：`dashboard/ui.py`
- 测试：`tests/test_minimal_dashboard_schema_migration.py`
- 测试：`tests/test_dashboard_projection.py`
- 测试：`tests/test_dashboard_ui.py`

**接口：**
- 消费：任务 3 的确认和近期目标多头摘要、任务 4 的方向关系。
- 产出：无多空字段、无分组收益、无 hash 的中文业务读模型。

- [ ] **步骤 1：写失败测试，拒绝公开多空字段和含糊回撤字段**

```python
def test_public_metrics_only_store_named_target_long_results() -> None:
    sql = MIGRATION_PATH.read_text("utf-8").lower()
    assert "target_long_annualized_return" in sql
    assert "target_long_max_drawdown" in sql
    assert "long_short" not in sql
    assert "q10_q1" not in sql
    assert " max_drawdown " not in sql
```

- [ ] **步骤 2：运行迁移、投影和 UI 测试并确认失败**

运行：`.venv/bin/python -m unittest tests.test_minimal_dashboard_schema_migration tests.test_dashboard_projection tests.test_dashboard_ui -v`

- [ ] **步骤 3：实现 PostgreSQL 四表重建与目标多头页面**

`factor_metrics` 保存确认期与近期的目标多头年化、回撤和 Sharpe，并保留内部治理所需的非空 `win_rate`；`factors` 保存事前方向、发现方向及“支持/反转/不明确”的中文说明。页面只画目标多头净值、回撤和基准，不展示多空组合或内部胜率来源序列。

- [ ] **步骤 4：运行 Dashboard 全页 AppTest**

运行：`.venv/bin/python -m unittest tests.test_dashboard_contract tests.test_dashboard_projection tests.test_dashboard_ui -v`

- [ ] **步骤 5：提交**

```bash
git add dashboard/migrations/012_rebuild_long_only_dashboard_schema.sql dashboard/pg_store.py dashboard/app.py dashboard/pages/1_批次总览.py dashboard/pages/3_分组回测.py dashboard/ui.py tests/test_minimal_dashboard_schema_migration.py tests/test_dashboard_projection.py tests/test_dashboard_ui.py
git commit -m "feat: simplify dashboard to target-long metrics"
```

### Task 6：服务器部署、29 因子重算与验收

**文件：**
- 修改：`deploy/systemd/factor-miner-dashboard.service`
- 修改：服务器私有运行配置（不进 Git）
- 生成：服务器正式 JSON/JSONL/Parquet 产物与 PostgreSQL 四表读模型

**接口：**
- 消费：当前 29 个正式候选 Spec，不修改公式和事前假设。
- 产出：新的已发布运行、huan001—huan029 中文目录、确认期与近期目标多头指标。

- [ ] **步骤 1：本地完整合成回归与静态检查**

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src dashboard tests
git diff --check
```

- [ ] **步骤 2：部署到独立版本目录并切换 current 链接**

使用仓库现有部署脚本；Dashboard 与 Worker 的 `PYTHONPATH` 必须指向新版本 `src`，私有 DSN、Token 和路径不得写入 Git 或日志。部署前把旧活动批次标识为待清除，禁止它在新协议下恢复。

- [ ] **步骤 3：执行迁移 012，重建空 PostgreSQL 读模型**

迁移前停止 Worker 与 Dashboard；导出当前 29 条 `source_candidate_id → huanNNN` 映射和 29 份 Spec。迁移后恢复该映射，确保同一公式与候选血缘继续使用原有 `huan001—huan029`；旧数据库不保留回滚副本。

- [ ] **步骤 4：用现有 29 个 CandidateSpec 执行正式重算**

真实运行必须使用沪深全市场、2021—2026 完整输入、2021—2023 方向发现、2024—2026 确认和 2025—2026 近期切片。不得自动执行 2012 年以来压力测试。

- [ ] **步骤 5：验收产物、数据库与页面**

```text
候选数 = 29
公开 factors 行数 = 29
公开 factor_metrics 行数 = 29
Dashboard 页面无 Q10-Q1、多空、worker、hash 或 snapshot 字样
每个因子有中文假设、机制、计算方法、事前方向、发现方向和方向关系
每个因子有确认期与近期目标多头年化、最大回撤和 Sharpe
方向决定只引用 2021—2023，确认指标只引用 2024—2026
普通运行没有长历史压力测试产物
```

- [ ] **步骤 6：删除旧协议数据**

只在新运行、29 行数据库和全部 Dashboard 页面均验收通过后，删除旧协议的 autonomous control 批次、旧 run 产物、旧审批对象、旧研究记忆和旧部署目录；保留 QuantLake、Barra 派生数据、新运行产物、29 份新协议候选对象及稳定编号映射。删除前必须用只读清单解析出每个精确目标，不允许对产物根目录做递归删除。

- [ ] **步骤 7：提交部署文件与验收记录**

```bash
git add deploy/systemd/factor-miner-dashboard.service docs/superpowers/plans/2026-08-17-factor-miner-long-only-five-year-rebuild.md
git commit -m "ops: deploy long-only five-year research protocol"
```
