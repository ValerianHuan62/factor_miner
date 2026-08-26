# 任务二报告：coverage graph 核验与三类 gap report

## 修改文件

- `src/factor_miner/research_gap.py`
- `tests/test_research_gap.py`
- `.superpowers/sdd/2026-08-06-factor-miner-memory-guided-hypothesis-evolution/task-2-report.md`

## 实现决定

- 新增 `load_verified_coverage_graph(root)`，严格只读取调用方显式传入的 graph root。
  - 先复用 `verify_coverage_graph` 做 manifest、文件集合、逐文件 hash 与目录身份核验。
  - 再解析 `build_spec.json`、`factor_nodes.jsonl`、各 parquet、`cluster_assignments.json`、`coverage_summary.json`。
  - 对以下边界做二次硬失败：
    - `build_spec` 与 manifest 的 `coverage_spec_id`、`regime_snapshot_id`、`factor_catalog_sha256`、`daily_ic_manifest_sha256`、区间、版本不一致；
    - `factor_nodes.factor_id`、`factor_performance.factor_id`、`regime_profiles(factor_id,state)`、边主键重复；
    - 结构边/信号边因子对不完整；
    - cluster 未覆盖全部因子或成员集合不一致；
    - `regime_profiles` 与 `regime_annotations` 的状态集合、中文标签不一致；
    - `coverage_summary` 的字段集合、聚合计数、均值映射、状态标签、cluster 数与正式文件不一致。

- 新增 `build_coverage_gap_report(graph, memory_snapshot, policy)`。
  - 先调用 `build_memory_snapshot_identity`，并核对 snapshot 的 `coverage_graph_id` / `coverage_graph_manifest_hash` 与当前 graph 完全一致。
  - 固定输出三类候选：
    - 结构空白：基于已观测字段 alias × 算子族 × 窗口带 × 输入组合的缺失组合；
    - 市场状态空白：基于 `regime_profiles` 的状态样本不足或高熵不稳定；
    - 数据可用性空白：基于 `factor_performance` 的无效日期/低覆盖率，以及不可比较的 signal edge 原因。
  - 对本地字段和算子做显式脱敏映射；遇到未登记字段或算子时用 `LLM_PRIVACY_VIOLATION` 硬失败，不做兜底。
  - 用 `gap_category`、内部风险等级、`canonical_gap_hash` 做稳定选择；最终再按公共 schema 要求的 `(gap_category, card_sha256, gap_id)` 排序。
- 最多输出 10 张卡；本轮兼容性补强后，`CoverageGapReport` 显式新增 `truncated_count` 与 `truncation_reason`，并参与 report identity。
  - 当三类候选整体为空时，返回 `COVERAGE_GAP_INVALID`。

- 新增 `build_sanitized_gap_brief(report, field_registry_hash, design_policy)`。
  - 只导出：
    - gap id
    - gap category / labels
    - allowed field aliases
    - allowed operator families
    - temporal window bins
    - required structure axes
    - data availability labels
    - allowed failure risk labels
    - design diversity contract
  - 递归扫描外发 payload；出现以下任一内容立即失败：
    - `rank_ic` / `icir` / `Sharpe` / `最大回撤` / `收益` 等历史评价词；
    - 日期串；
    - 本地或服务器路径；
    - ticker 样式文本；
    - 未登记公开文本；
    - 未登记对象类型。

## 测试命令与实际输出

命令：

```text
env UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest tests.test_research_gap tests.test_coverage_snapshot tests.test_coverage_schema
```

实际输出：

```text
..............
----------------------------------------------------------------------
Ran 14 tests in 0.078s

OK
```

## concern 修复追加说明

- 兼容性补强了任务一公共 schema：
  - 在 `src/factor_miner/research_evolution_schema.py` 的 `CoverageGapReport` 中新增 `truncated_count: int` 与 `truncation_reason: str`；
  - 两字段都参与 `report_sha256` 内容身份计算；
  - `truncated_count == 0` 时强制要求 `truncation_reason == "未发生截断"`；
  - `truncated_count > 0` 时强制要求 `truncation_reason` 非空。
- `build_coverage_gap_report` 现在显式写入：
  - 未截断：`0 / "未发生截断"`；
  - 发生截断：真实截断数量 / 明确稳定截断原因。
- 更新了：
  - `tests/test_research_evolution_schema.py`：断言新字段校验与 hash 合同；
  - `tests/test_research_gap.py`：断言截断场景下 `truncated_count` 大于 0、`truncation_reason` 正确，且外发 brief 仍不暴露内部评价数值或截断内部细节。

## 修复后覆盖测试命令与实际输出

命令：

```text
env UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest tests.test_research_gap tests.test_coverage_snapshot tests.test_coverage_schema tests.test_research_evolution_schema
git diff --check
```

实际输出：

```text
......................
----------------------------------------------------------------------
Ran 22 tests in 0.075s

OK
```

## 修复轮 1 追加说明

- 修复了截断场景下“三类 gap 覆盖要求不成立”的问题：
  - `_select_gap_candidates()` 现在先验证三类候选是否齐全；
  - 若 `max_cards < 3`，直接以 `COVERAGE_GAP_INVALID` 硬失败；
  - 若任一类别无候选，直接以 `COVERAGE_GAP_INVALID` 硬失败并在消息中指出缺失类别；
  - 当三类都存在且预算允许时，优先为 `structural`、`market_regime`、`data_availability` 各保留一张，再按固定风险顺序和 `canonical_gap_hash` 填充剩余名额；
  - 成功截断时继续显式记录真实 `truncated_count` 与 `truncation_reason`，并保持 report identity 可复验。

- 补充测试：
  - 截断后返回的 `gap_cards` 必须仍覆盖三类；
  - `max_cards < 3` 必须失败；
  - 某一类候选缺失必须失败。

## 修复轮 1 测试命令与实际输出

命令：

```text
env UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest tests.test_research_gap tests.test_coverage_snapshot tests.test_coverage_schema tests.test_research_evolution_schema
git diff --check
```

实际输出：

```text
........................
----------------------------------------------------------------------
Ran 24 tests in 0.089s

OK
```

## concerns

- `regime_snapshot_hash` 公共 schema 需要 hash，但现有 coverage graph manifest 未直接暴露 regime snapshot manifest hash；本实现继续使用 `regime_snapshot_id + regime_annotations` 的规范内容哈希作为稳定替代身份。
