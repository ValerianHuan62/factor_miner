# Task 4：方向发现写入不可变研究记忆与因子图谱

## 结论边界

方向发现只冻结发现窗口的离散方向，供确认期和因子图谱取向使用。事前假设方向不被改写；`reversed` 仅说明该事前方向在发现区间被反转，不能构成机制认证、有效 Alpha 或生产结论。

## RED

- 新增轻量记忆与图谱反转测试后，`LightweightMemoryEntry` 缺少 `hypothesis_direction`，测试以 `AttributeError` 失败，确认测试能捕获回读 `HypothesisSpec` 的错误实现。
- 新增正式记忆反转测试后，中文否证说明为 `None`，确认正式记忆入口尚未保留反转关系。

## GREEN

- 正式和轻量不可变记忆追加 `hypothesis_direction`、`selected_direction`、`direction_relation`、`direction_source` 与冻结方向记录哈希；方向字段参与内容哈希。
- `DirectionDecision` 是唯一的轻量方向来源；服务器读取并校验已发布 Pilot 的 `direction/decisions.json` 及其清单哈希后才传递该决定。
- 反转记忆保留中文说明：`事前负向假设在方向发现区间被反转，确认结果另行记录`。
- 图谱 `orientation_sign` 取冻结 `selected_direction`，`orientation_source` 固定为 `discovery_window_frozen`；没有冻结方向决定的候选不进入该图谱。
- 验证：`.venv/bin/python -m unittest tests.test_long_only_protocol tests.test_lightweight_evolution tests.test_research_evolution tests.test_research_evolution_schema tests.test_research_memory tests.test_research_memory_projection tests.test_server_research_dependencies -v`（72 项通过）。
- 验证：`.venv/bin/python -m unittest tests.test_coverage_catalog tests.test_coverage_cluster tests.test_coverage_performance tests.test_coverage_regime tests.test_coverage_signal tests.test_coverage_snapshot tests.test_coverage_structure -v`（23 项通过）。
- 验证：`.venv/bin/python -m compileall -q src/factor_miner` 与 `git diff --check` 通过。

## 提交

- Commit：`feat: remember discovered factor direction`。

## 风险

- 未在 Mac 使用真实市场数据；真实服务器运行仍须由公司 Linux 环境、已冻结 Pilot 产物和数据合同完成。
- 轻量记忆对象的内容字段已扩展，部署按任务约定清理旧协议记忆后再开始新轮次。

## 复审修复一

### RED

- `unresolved` 三态测试失败：旧 schema 仍只允许 `inconclusive`，且未取得冻结决定的候选方向字段全部为 `None`。
- Worker 磁盘方向产物测试失败：旧接口读取内存 `pilot.manifest`，没有从正式运行目录重验清单、方向决定版本和方向记录绑定。

### GREEN

- 正式与轻量记忆统一使用 `supported`、`reversed`、`unresolved`；未决项保留事前方向、`selected_direction=null`、`discovery_window_unresolved` 和中文说明，且参与内容哈希。
- 图谱仍仅消费 `discovery_window_frozen` 的决定；未决候选不会获得取向节点。
- Worker 先以 `verify_published_run` 从磁盘重新核验正式 manifest，再校验 `direction/decisions.json` 版本、候选和 Spec、记录引用及哈希；随后重读状态目录记录，核验方向决定、候选/Spec 和完整 provenance 与运行指标一致。
- 新增负测覆盖旧方向产物版本、记录引用不一致、记录哈希不一致、已发布方向产物篡改，以及旧 manifest 缺少方向产物。

### 验证

- `.venv/bin/python -m unittest tests.test_lightweight_evolution tests.test_research_evolution_schema tests.test_research_memory tests.test_research_memory_projection tests.test_research_evolution tests.test_server_research_dependencies -v`：68 项通过。
- `.venv/bin/python -m compileall -q src/factor_miner` 与 `git diff --check` 通过。

### 提交

- Commit：`fix: verify remembered direction provenance`。
