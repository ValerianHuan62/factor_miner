# Factor Miner V0.4 双层因子覆盖图谱实施计划

> **代理执行要求：** 按任务逐项实施；每个行为变化使用测试先行，每个任务测试通过后单独提交。

**目标：** 从现有生产因子 YAML、冻结每日 RankIC 和 V0.3 状态快照确定性构建内容寻址的双层因子覆盖图谱。

**架构：** YAML 导入、领域合同、逐对统计、状态画像、聚类和快照发布分成独立模块。真实结果只在公司 Linux 生成；Mac 只验证纯合成数据和确定性失败语义。

**技术栈：** Python 3.12、Pydantic、Polars、SciPy、PyYAML、Typer、JSON/JSONL、Parquet。

## 全局约束

- 所有 Markdown、注释和文档字符串使用中文。
- 所有每日 IC 相关必须逐因子对计算，禁止宽矩阵批量相关。
- 市场状态只读取 filtered probability，并按 `earliest_use_date` 对齐。
- 中文状态名属于指定快照的人工注释，不能自动跨快照继承。
- 真实数据、完整 IC、完整图谱和私有路径不进入 Git。
- 每个任务遵循失败测试、最小实现、通过测试、独立提交的顺序。

---

### 任务 1：冻结覆盖图谱合同与 YAML 目录导入

**文件：**

- 修改：`pyproject.toml`
- 修改：`uv.lock`
- 新建：`src/factor_miner/coverage_schema.py`
- 新建：`src/factor_miner/coverage_catalog.py`
- 新建：`tests/test_coverage_schema.py`
- 新建：`tests/test_coverage_catalog.py`

**接口：**

- 产出：`CoverageGraphSpec`、`CoveragePairPolicy`、`CoverageStructuralPolicy`、`CoverageFactorNode`。
- 产出：`load_legacy_factor_catalog(path: Path) -> tuple[CoverageFactorNode, ...]`。

- [x] 写 `CoverageGraphSpec` 内容寻址、状态注释唯一性、日期区间和冻结阈值的失败测试。
- [x] 运行目标测试，确认因缺少模块而失败。
- [x] 实现不可变 Pydantic 合同和稳定 `coverage_spec_id`。
- [x] 写生产 YAML 最小样例测试，覆盖字段、类别、自由文本公式、算子标记、窗口和负 IC 方向。
- [x] 运行测试，确认因缺少 YAML 导入而失败。
- [x] 使用 `yaml.safe_load` 实现导入；拒绝重复 ID、未知顶层结构、空字段和非有限历史指标。
- [x] 运行 `tests/test_coverage_schema.py` 与 `tests/test_coverage_catalog.py`。
- [x] 提交 `feat: freeze v0.4 coverage contracts`。

### 任务 2：实现逐因子对每日 IC 相关

**文件：**

- 修改：`src/factor_miner/errors.py`
- 新建：`src/factor_miner/coverage_signal.py`
- 新建：`tests/test_coverage_signal.py`

**接口：**

- 产出：`DailyICIdentity`、`SignalPatternEdge`。
- 产出：`compare_daily_ic_pair(a, b, node_a, node_b, policy) -> SignalPatternEdge`。
- 产出：`build_signal_pattern_edges(nodes, daily_ic, identity, policy) -> tuple[SignalPatternEdge, ...]`。

- [x] 写单个因子对逐日期交集的失败测试，手算原始 Spearman 和方向对齐 Spearman。
- [x] 运行测试，确认因缺少实现而失败。
- [x] 实现单对函数，只接受两条长序列，不接受宽矩阵。
- [x] 写不同缺失模式、最小重叠、最大缺失和身份不兼容测试。
- [x] 运行测试，确认每个错误分支先失败。
- [x] 实现明确的 `comparable` 与 `reason`，禁止填零。
- [x] 写三个因子的组合测试，并通过测试注入的 pair comparator 记录恰好调用三次且每次只有两个因子。
- [x] 实现按排序后无序因子组合逐对调用。
- [x] 运行目标测试并提交 `feat: compute daily ic correlations pair by pair`。

### 任务 3：实现数学结构关系和双层确定性聚类

**文件：**

- 新建：`src/factor_miner/coverage_structure.py`
- 新建：`src/factor_miner/coverage_cluster.py`
- 新建：`tests/test_coverage_structure.py`
- 新建：`tests/test_coverage_cluster.py`

**接口：**

- 产出：`StructuralEdge`、`compare_factor_structure`、`build_structural_edges`。
- 产出：`CoverageClusterAssignments`、`build_coverage_clusters`。

- [x] 写精确公式、字段重叠、算子重叠、窗口邻近和类别关系的手算测试。
- [x] 运行测试确认失败，随后实现冻结加权相似度。
- [x] 写“结构相似但信号不同”和“公式不同但信号相似”的双层分簇测试。
- [x] 运行测试确认失败，随后实现按因子 ID 稳定排序的连通分量。
- [x] 运行两个目标测试并提交 `feat: build structural and signal clusters`。

### 任务 4：实现中文状态标签和概率加权状态画像

**文件：**

- 新建：`src/factor_miner/coverage_regime.py`
- 新建：`tests/test_coverage_regime.py`
- 修改：`docs/V0.3市场状态金融流程说明.md`
- 修改：`docs/superpowers/plans/2026-07-29-factor-miner-v0.3-walk-forward-hmm-market-regime.md`

**接口：**

- 产出：`RegimeFactorProfile`。
- 产出：`build_regime_profiles(nodes, daily_ic, filtered_regimes, annotations, policy)`。

- [x] 写合成测试，令 `observation_date` 与 `earliest_use_date` 指向不同状态，证明实现只能按后者连接。
- [x] 运行测试确认失败，随后实现概率加权均值、标准差、ICIR、有效概率质量和不确定性摘要。
- [x] 写注释缺失、重复状态名、快照 ID 不匹配、概率维度不符和 `unavailable` 的失败测试。
- [x] 实现失败关闭和中文标签输出。
- [x] 将服务器验收文档中的展示名称改为“高波动下跌·弱宽度”和“低波动平稳·宽度中性”，括号保留 canonical ID。
- [x] 运行目标测试并提交 `feat: profile factors by named market regimes`。

### 任务 5：实现不可变图谱快照

**文件：**

- 新建：`src/factor_miner/coverage_snapshot.py`
- 新建：`tests/test_coverage_snapshot.py`

**接口：**

- 产出：`CoverageGraphManifest`、`CoverageGraphBuildResult`。
- 产出：`publish_coverage_graph(...) -> CoverageGraphBuildResult`。
- 产出：`verify_coverage_graph(snapshot_root: Path) -> CoverageGraphBuildResult`。

- [x] 写完整文件集合、逐文件哈希、目录身份和字节篡改测试。
- [x] 运行测试确认失败，随后实现 staging、原子发布和内容寻址 manifest。
- [x] 写相同输入幂等、任一输入变化产生新 ID 的测试。
- [x] 实现稳定 JSON/JSONL、确定性 Parquet 排序和覆盖摘要。
- [x] 运行目标测试并提交 `feat: publish immutable coverage graph snapshots`。

### 任务 6：实现 CLI 与合成端到端

**文件：**

- 修改：`src/factor_miner/cli.py`
- 新建：`src/factor_miner/coverage_workflow.py`
- 新建：`tests/test_coverage_synthetic_e2e.py`
- 修改：`tests/test_cli.py`
- 新建：`examples/coverage/coverage_graph_spec_v0.4.json`

**接口：**

- 产出：`factor-miner coverage build`。
- 产出：`factor-miner coverage verify`。

- [x] 写纯合成端到端失败测试，覆盖 YAML、长表 IC、已发布合成状态快照、双层边、中文状态画像和快照核验。
- [x] 运行测试确认失败，随后编排导入、逐对计算、状态画像、聚类和发布。
- [x] 写 CLI build/verify 成功与损坏快照失败测试。
- [x] 实现 `coverage` Typer 子命令和机器可读输出。
- [x] 运行端到端与 CLI 测试并提交 `feat: complete factor miner v0.4 local workflow`。

### 任务 7：完整验证、中文说明与公司 Linux 验收

**文件：**

- 新建：`docs/V0.4因子覆盖图谱金融流程说明.md`
- 修改：`docs/START_HERE.md`
- 修改：`docs/superpowers/plans/2026-07-28-factor-miner-v0.3-v0.5-regime-coverage-and-llm.md`
- 修改：本计划

- [x] 运行格式、导入边界、全部本地测试和 CLI 帮助，确认 Mac 未读取真实结果。
- [x] 用当前代码提交在公司 Linux 锁定环境运行完整测试。
- [x] 使用服务器 `/data/quantlake` 派生产物、现有生产 YAML、冻结评价政策和 `regsnap_12b8bd1c99790513d1dd4941` 构建真实样例。
- [x] 核对逐对数量为 \(N(N-1)/2\)、不可比较原因、状态中文名、图谱文件哈希和重复 verify。
- [x] 在计划末尾记录非敏感验收：代码提交、测试数、因子数、因子对数、状态标签、图谱 ID 和聚合诊断；不记录完整 IC、完整图谱或私有路径。
- [x] 提交 `docs: record v0.4 server acceptance`。

## 公司 Linux 验收记录

- 验收日期：2026-07-29。
- 图谱构建代码提交：`17ac66a`。
- 本地与公司 Linux：各 232 项测试通过；CLI 帮助和导入边界检查通过。
- 冻结市场状态快照：`regsnap_12b8bd1c99790513d1dd4941`。
- 覆盖图谱：`covgraph_3e3b6821f534dad40b7369d7`。
- 覆盖规格：`covspec_782b190599acd3d7f5eafdad`。
- 因子数：216；无序因子对数：23,220，与 \(216\times215/2\) 一致。
- 可比较信号对：16,290；不可比较信号对：6,930，全部因为重叠有效 IC 日期少于冻结阈值。
- 不可评价因子：35。诊断中 30 个上游原始列在可见区间无有限值，另 5 个每日截面为常数；均保留节点且未补零。
- 强信号模式边：6,753；强结构边：883；信号簇 41 个；结构簇 20 个。
- 中文状态名：`高波动下跌·弱宽度`、`低波动平稳·宽度中性`。
- 图谱 8 个正式内容文件逐一通过 SHA-256 核验；重复执行 `coverage verify` 返回同一图谱 ID。
- 以上仅为可见区间覆盖图谱的工程与数据验收，不构成因子有效性、机制认证、生产入库或密封样本外结论。
