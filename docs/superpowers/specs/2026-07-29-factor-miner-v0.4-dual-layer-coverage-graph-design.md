# Factor Miner V0.4 双层因子覆盖图谱技术设计

- 状态：已批准，进入实施
- 日期：2026-07-29
- 前置版本：V0.3 走步式 HMM 市场状态快照

## 1. 金融目标与结论边界

V0.4 不负责寻找“最佳因子”，而是用冻结、可复核的历史资料回答三个问题：

1. 现有因子库在字段、公式结构、窗口和人工经济类别上覆盖了什么；
2. 公式不同的因子是否表现出相似的逐日 RankIC 模式；
3. 因子在已经冻结的市场状态下是否呈现不同的历史表现。

覆盖稀疏不等于存在 Alpha，逐日 IC 高相关不等于经济机制相同，状态条件表现差异也不构成状态择时证据。V0.4 产物只能称为“可见区间因子覆盖图谱”，不能称为因子有效性、机制认证或生产结论。

## 2. 输入、观察时点与不变量

### 2.1 因子目录

V0.4 直接读取现有生产因子 YAML。现有 YAML 保存自由文本公式，而不是 Factor Miner 类型化 AST，因此必须明确区分两类结构来源：

- `typed_ast`：Factor Miner 原生候选，结构来自规范 AST；
- `legacy_formula_metadata`：现有生产因子，结构来自 `formula_expr`、`input_fields`、`category`、`subcategory`、`lookback_window` 和 `param_n`。

固定程序可以从自由文本公式中识别冻结白名单里的算子标记和窗口数字，但不能把自由文本重新解释成可执行代码，也不能声称它等价于类型化 AST。未经核验的自由文本公式不得执行。

YAML 读取使用 `PyYAML.safe_load`。采用时记录：官方仓库 `yaml/pyyaml`，约 2.9k GitHub star，最近稳定版 `6.0.3` 于 2025-09-25 发布，PyPI 标记为 Production/Stable；项目固定 `PyYAML>=6.0.3,<7`。

### 2.2 每日 IC

服务器输入使用长表：

```text
factor_id
date
rank_ic
coverage
evaluation_policy_id
data_release_id
label_id
visible_start
visible_end
```

每个因子的逐日 RankIC 必须由相同的股票池、标签、信号滞后、可见区间和评价政策生成。完整 IC 序列是公司内部结果，不进入 Git，也不得发送给外部 LLM。

### 2.3 市场状态

V0.4 只读取已核验、冻结的 V0.3 `RegimeSnapshot`。日期 `t-1` 收盘后的 filtered probability 通过 `earliest_use_date=t` 对齐到日期 `t` 的因子观察，不允许按 `observation_date=t` 直接连接。

中文经济标签是引用具体 `regime_snapshot_id` 的人工注释，不是 HMM 自动生成的事实。当前正式中位数收益轨道的展示标签冻结为：

- `canonical_state_id=0`：`高波动下跌·弱宽度`；
- `canonical_state_id=1`：`低波动平稳·宽度中性`。

数字 ID 只保留为机器审计键。新状态快照不得自动继承旧快照标签。

### 2.4 状态数说明

中位数收益轨道的正式 `K=2` 不是只由 BIC 决定。冻结排序依次比较：

1. 走步式样本外对数似然；
2. 月度成功率和跨月映射稳定性；
3. 状态经济区分度；
4. BIC。

研究中部分 `K=3` 候选的 BIC 更低，但样本外对数似然和稳定性更差，因此中位数轨道选择 `K=2`。等权收益轨道选择的是 `K=3`，说明状态数是特征轨道和完整验证共同决定的，不是全系统预设。

## 3. 冻结合同

### 3.1 `CoverageGraphSpec`

结果揭晓前登记并内容寻址：

```text
spec_version
factor_catalog_sha256
daily_ic_manifest_sha256
regime_snapshot_id
regime_annotations
evaluation_policy_id
data_release_id
label_id
visible_start
visible_end
pair_policy
structural_policy
cluster_policy
algorithm_version
random_seed
runtime_provenance
created_at
```

`pair_policy` 冻结：

- `correlation_method=spearman`；
- `min_overlap_dates=60`；
- `max_missing_ratio=0.20`；
- `direction_alignment=expected_sign`；
- `strong_signal_correlation=0.70`。

`structural_policy` 冻结字段、算子、窗口、类别各自的 Jaccard 或邻近权重。规范 AST 完全一致单独标记，不能被加权平均掩盖。

### 3.2 因子节点

节点至少保存：

```text
factor_id
factor_name_cn
node_status
structure_source
formula_expr
formula_hash
ast_hash
input_fields
operator_tags
windows
lookback_window
lag_days
category
subcategory
expected_sign
preprocess_method
neutralization
mean_rank_ic
icir
positive_rank_ic_ratio
median_coverage
annual_summaries
incremental_summary
source
parent_factor_ids
research_family_id
```

YAML 中旧的 `mean_ic`、`icir` 只能作为来源记录。正式节点历史表现必须从本次冻结每日 IC 长表重新汇总，并保留差异诊断。

## 4. 逐因子对信号模式计算

禁止先把所有因子 IC 序列变成宽矩阵后调用 `.corr()`、`numpy.corrcoef` 或等价批量接口。实现必须是显式双层因子对循环，每一对调用一次独立函数：

```text
compare_daily_ic_pair(factor_a_series, factor_b_series, identity, policy)
```

每个因子对独立执行：

1. 校验评价政策、数据发布、标签和可见区间完全一致；
2. 只读取该对的两条序列；
3. 按日期一对一内连接；
4. 删除该对自己的非有限值；
5. 计算交集日期数、并集日期数和缺失比例；
6. 检查最小重叠与最大缺失比例；
7. 计算原始有符号 Spearman 相关；
8. 按两个因子的 `expected_sign` 分别对齐，再计算经济方向一致的相关；
9. 写出该对自己的诊断和不可比较原因。

即使以后为了读取性能将长表按因子分区，也不得改变逐对统计语义。

## 5. 数学形式层

每个无序因子对分别计算：

- `exact_ast_match`；
- `exact_formula_match`；
- 字段 Jaccard；
- 算子 Jaccard；
- 窗口邻近度；
- category 和 subcategory 是否相同；
- 冻结加权结构相似度。

类型化 AST 与自由文本公式不能做“精确 AST 相等”。公式相似但信号模式不同的因子仍保留结构边，不强制并簇。

## 6. 市场状态条件画像

对每个因子和每个 canonical 状态，使用对齐到 IC 日期的 filtered probability \(p_{t,k}\) 计算：

\[
\bar{IC}_{k} =
\frac{\sum_t p_{t,k}IC_t}{\sum_t p_{t,k}}
\]

同时保存：

- 有效日期数；
- 概率质量 \(\sum_t p_{t,k}\)；
- 概率加权标准差和 ICIR；
- 正 IC 概率占比；
- 平均最大状态概率和平均熵；
- 中文经济标签及人工注释。

缺少状态标签、概率维度不符、`unavailable`、日期未按 `earliest_use_date` 对齐或有效概率质量不足时失败关闭。

## 7. 聚类与覆盖摘要

V0.4 使用确定性连通分量，不引入为了画图而不稳定的随机降维：

- 结构簇：`exact_ast_match`、`exact_formula_match` 或结构相似度达到冻结阈值的边；
- 信号簇：可比较且方向对齐相关不低于 `0.70` 的边。

结构簇与信号簇分别保存，绝不因为其中一层相似就覆盖另一层。因子 ID 排序决定遍历顺序和稳定簇 ID；`random_seed=0` 进入合同但算法不消费随机数。

覆盖摘要只描述：

- 类别、字段、算子、窗口和结构簇的数量；
- 信号簇及代表因子；
- 各状态下表现相对薄弱或缺失的簇；
- 失败、不可比较和低重叠数量。

V0.4 不生成 LLM 提示，也不提出新因子。

## 8. 不可变产物

`CoverageGraphSnapshot` 原子发布：

```text
build_spec.json
factor_nodes.jsonl
structural_edges.parquet
signal_pattern_edges.parquet
factor_performance.parquet
regime_profiles.parquet
cluster_assignments.json
coverage_summary.json
manifest.json
```

`manifest.json` 保存逐文件 SHA-256、输入身份、可见截止日、算法版本和 `coverage_graph_id`。任何因子、每日 IC、状态快照、标签、政策或文件字节改变都生成新的 ID。核验失败不得静默重算或继续供 V0.5 使用。

`factor_performance.parquet` 保存由本次冻结每日 IC 重新计算的平均 IC、ICIR、方向比例、覆盖率和分年表现。YAML 旧摘要只作为差异对账字段。`daily_ic_manifest_sha256` 在 V0.4 CLI 中绑定每日 IC Parquet 的完整文件字节，避免路径相同但内容被替换。

## 9. CLI 与运行边界

正式入口：

```bash
factor-miner coverage build coverage_graph_spec.json \
  --catalog factors_final_csv.yaml \
  --daily-ic daily_rank_ic.parquet \
  --regime-snapshot /data/factor_miner_artifacts/artifacts/regimes/<快照ID> \
  --artifact-root /data/factor_miner_artifacts

factor-miner coverage verify \
  /data/factor_miner_artifacts/artifacts/coverage_graphs/<图谱ID>
```

真实构建只允许公司 Linux。Mac 只运行程序生成的合成目录、合成 IC 和合成状态概率测试。核心包不得 import `huan_quant`，现有 YAML 只是符合本设计合同的外部输入。

## 10. 验收标准

- 当前正式状态在展示层只出现中文经济标签，数字 ID 仅作审计字段；
- 相同公式或 AST 的因子进入同一结构簇；
- 公式不同但逐日 IC 高相关的因子形成强信号边；
- 公式相似但表现不同的因子不被强制合并为信号簇；
- 符号相反但经济方向相同的因子可通过方向对齐形成强边；
- 每个 IC 因子对独立对齐、独立处理缺失、独立计算 Spearman；
- 评价身份不兼容、重叠短或缺失严重时明确不可比较；
- 状态概率只按 `earliest_use_date` 使用；
- 任一输入字节变化都会改变图谱 ID；
- Mac 合成端到端通过，公司 Linux 真实样例和完整测试通过。
