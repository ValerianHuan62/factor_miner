# Factor Miner V0.1 可信可见验证技术设计

- 状态：已批准实施
- 日期：2026-07-17
- 替代范围：V0 正式可见验证的运行语义；V0 历史账本和产物保持不可变

## 1. 结论

V0 已证明独立项目、类型化 DSL、确定性编译、JSONL 账本和合成测试路线可行，但还没有证明正式可见验证结论可信。V0.1 不增加在线大语言模型、RMA、经验记忆、进化搜索或 TreeSHAP；它只修复会改变候选准入结果、破坏结果隔离或使运行无法审计的 P0 缺口。

V0.1 的唯一终点仍是“通过可信可见验证的候选因子”。它不是认证因子、Evidence、生产因子或实盘许可。

## 2. 审计输入与保留项

本设计吸收 2026-07-17 实盘前审计的七项结论：

1. 正式 CLI 可绕过结构和输出冗余闸门。
2. 标签、掩码、股票池、信号时间和统计阈值没有成为版本化评价协议。
3. 行情输入与研究结果共用一个 `DataSource`，编译前已经打开标签。
4. 可见验证可指向冒烟测试输入，清单、代码和配置哈希只校验非空。
5. 回看窗口把交易观察数错误当成自然日天数。
6. 通过所需的执行计划、质量检查、评价指标、冗余结果和候选包没有完整原子落盘。
7. 未来自适应搜索不能按单个批次重置多重检验预算。

以下 V0 决策保持不变：独立于 `huan_quant`；CLI 是正式入口；只允许白名单类型化 AST；不执行任意 Python；真实研究只在公司 Linux 服务器；原始数据和结果不进入 Git；JSON/JSONL 继续作为 V0.1 正式存储。

## 3. 范围

### 本版包含

- 版本化、内容寻址的 `EvaluationPolicySpec`
- 类型化 `AvailabilitySpec` 与时点正确的 `UniverseSpec`
- `FactorInputSource` / `OutcomeSource` / `ReferenceFactorSource` 物理接口分离
- 按交易日观察数预热，不再使用 `timedelta(days=lookback)`
- 可见验证与冒烟测试路径隔离，以及实际来源和哈希校验
- 编译后、打开结果前的前缀截断与未来输入扰动测试
- 工作流中强制执行结构冗余和输出冗余
- 完整、内容寻址、原子发布的运行产物
- 中断检测与显式 `interrupted` 终态
- 跨周期 `ResearchFamilySpec` 与不可回收假设名额的最小账本语义

### 本版不包含

- 在线大语言模型、提示词或外部模型调用
- RMA、宏观/微观/交叉研究脑、经验记忆和 FAA
- 变异、交叉、修复和新颖性注入
- TreeSHAP、模型训练和贝叶斯优化
- 密封样本外检验、机制认证、组合回测、成本、容量、模拟交易或实盘交易
- PostgreSQL、多写入器、任务队列或分布式工作进程

## 4. 信任边界与阶段状态机

```text
候选/批次/研究族/政策已登记
  → 结构校验 + DSL 校验 + 编译
  → 结构冗余检查
  → FactorInputSource.inspect_inputs
  → 动态未来依赖探针
  → 原始因子计算 + 质量检查
  → 准备 ReferenceFactorSource 输出冗余输入
  → OutcomeSource.inspect_outcomes / scan_outcomes   # 首次接触研究结果
  → RankIC + HAC + Bonferroni + 输出冗余检查
  → 完整产物写入暂存目录，fsync 后重命名为最终目录
  → 账本终态事件
```

在 `OutcomeSource` 首次打开前，进程不得读取标签文件、标签结构、标签截止日、标签哈希或包含标签的联合面板。冒烟测试只使用 `FactorInputSource`，永远不构造 `OutcomeSource`。

## 5. 冻结对象

### 5.1 可得性规范 `AvailabilitySpec`

V0.1 的 `TrustedCandidateFactorSpec` 不接受自由文本 `availability`。公司日频 V0.1 固定表达：观察到 `close_t` 后形成信号，最早在 `open_t_plus_1` 交易。字段可得性由数据合同中的逐字段时点表验证。旧 `CandidateFactorSpec(spec_version=1)` 作为只读历史类型保留，不允许进入 `TrustedVisibleCampaignSpec`；迁移会生成新的 `spec_version=2` 和候选 ID。

### 5.2 股票池规范 `UniverseSpec`

评价协议必须声明时点正确的股票池：稳定的股票池 ID、版本、成员列、成员可得时点和 `point_in_time=true`。V0.1 公司 A 股策略固定使用状态表当日可得的全 A 股候选集合，不允许使用期末成分股回填历史。

### 5.3 评价政策规范 `EvaluationPolicySpec`

评价政策采用内容寻址并独立登记。它冻结：

- 标签 ID、列名、未来期限和公式版本
- 排名掩码与时点正确的股票池
- 信号观察时间和最早交易时间
- 显著性水平、HAC 规则与最大滞后、最少日期数、股票数和覆盖率
- 效果量闸门（平均 RankIC 绝对值下限）
- 中性化政策（V0.1 为 `none`，必须显式）
- 输出冗余阈值与参考池清单 ID

`TrustedVisibleCampaignSpec` 只引用 `evaluation_policy_id`，不能覆盖上述字段。旧 `CampaignSpec` 只用于解析 V0 历史记录。公司 V0.1 只内置一个经过代码和数据合同共同约束的政策；自定义政策属于后续版本。

HAC 与 Bonferroni 服务于当前的逐日 RankIC 推断：前者处理 RankIC 序列的时间依赖，后者控制预登记因子研究族的多重检验。V0.1 不计算 CPCV 或 Deflated Sharpe Ratio。CPCV 属于带训练/测试切分的模型或策略稳定性评估；Deflated Sharpe Ratio 依赖策略收益及其 Sharpe Ratio、偏度、峰度、样本长度和试验集合。原始因子尚未形成持仓和策略收益，因此不能用这两项指标替代因子 IC 的多重检验。

### 5.4 研究族规范 `ResearchFamilySpec`

研究族在任何结果前冻结 `research_family_id`、全局假设预算和政策 ID。每个候选、参数变体、修复或变异都占用一个原子名额；失败和未运行名额不回收。V0.1 没有自动进化，但先建立跨批次预算语义，Bonferroni 检验族大小取研究族的全局预算，不再取单次批次自报数字。

## 6. 数据端口

```python
class FactorInputSource(Protocol):
    def inspect_inputs(self) -> InputProvenance: ...
    def scan_inputs(self, request: FactorInputRequest) -> pl.LazyFrame: ...

class OutcomeSource(Protocol):
    def inspect_outcomes(self, policy: EvaluationPolicySpec) -> OutcomeProvenance: ...
    def scan_outcomes(self, request: OutcomeRequest) -> pl.LazyFrame: ...

class ReferenceFactorSource(Protocol):
    def inspect_references(self, manifest_id: str) -> ReferenceProvenance: ...
    def scan_reference(self, factor_id: str, start: date, end: date) -> pl.LazyFrame: ...
```

`FactorInputRequest.warmup_observations` 表示每个资产需要的历史交易观察数。适配器必须使用交易日历向前解析读取起点，并在收集数据后验证每个资产在可见验证开始日前具有足够历史行；不足时硬失败，不缩短滚动窗口。

## 7. 真实运行与数据来源

冒烟测试可以读取 `/data/factor_miner_artifacts/inputs` 下的有界输入。可见验证的行情和状态 URI 必须位于配置的 QuantLake 发布根目录；标签必须位于版本化标签发布根目录；参考因子必须来自显式且带哈希的参考清单。可见验证禁止任何 URI 位于冒烟测试输入根目录。

真实运行配置必须提供并验证：

- 发布清单实际文件及其 SHA-256
- 解析后的发布 ID 与清单内容一致
- Git HEAD 与 `FM_CODE_COMMIT` 一致且工作区干净
- 私有配置规范哈希与 `FM_CONFIG_HASH` 一致
- `uv.lock` 实际 SHA-256
- 行情、状态、标签和参考文件或数据集清单哈希
- 数据结构、截止日、复权口径、交易日历和状态版本

字符串格式正确但无法与实际文件核对时，可见验证失败。诊断命令分别输出输入、结果和参考因子三类合同状态，不读取样本值或运行因子。

## 8. 动态未来依赖检测

类型化 DSL 仍是第一道防线。每个编译计划还必须在打开研究结果前通过两种确定性测试：

1. 前缀截断：分别在多个检查点截断输入，截断前的因子值必须与完整输入一致。
2. 未来扰动：只扰动检查点之后所有允许输入字段，检查点及之前的因子值必须逐值相同（空值位置也相同）。

探针使用实际 `FactorInputSource` 的行情和状态，不使用标签。检查点、随机种子、比较容差和输入哈希写入产物。任何变化都以 `LOOKAHEAD_DETECTED` 失败；系统不自动修复表达式。

## 9. 冗余闸门

结构冗余在打开真实输入前执行，比较参考因子的编译元数据和批次中排在当前候选之前的候选。缺少任一冻结参考元数据时失败。

输出冗余在原始因子已生成、结果评价完成后执行。参考池非空时，CLI 必须从 `ReferenceFactorSource` 加载全部参考因子；缺失、哈希不符、重叠不足或未执行都不能进入 `visible_passed`。批次内候选按事前顺序增量加入比较池，失败候选也保留其试验记录和名额。

## 10. 运行产物与崩溃语义

每次运行先写入 `artifacts/.staging/<run_id>`：

```text
run_manifest.json
candidates/<candidate_id>/execution_plan.json
candidates/<candidate_id>/lookahead_probe.json
candidates/<candidate_id>/raw_factor.parquet
candidates/<candidate_id>/quality.json
candidates/<candidate_id>/evaluation.json
candidates/<candidate_id>/inference.json
candidates/<candidate_id>/redundancy.json
candidates/<candidate_id>/candidate_package.json
```

所有文件写完后逐个计算哈希，对文件和目录执行 `fsync`，再原子重命名到 `artifacts/runs/<run_id>`。只有最终目录和完整清单存在后，才追加 `visible_passed` 或失败终态事件。启动新运行前扫描暂存目录与无终态运行，追加 `interrupted` 事件；不删除残留目录。

候选包必须声明 `visible_only=true`、`sealed_oos_used=false`、`production_eligible=false` 和 `mechanism_status=mechanism_unverified`。

## 11. 兼容与迁移

- V0 候选、批次、账本和产物不修改、不删除。
- 第一次 V0 可见验证批次标记为 `legacy_untrusted_visible`，不能被 V0.1 当作通过参考。
- V0.1 使用新结构版本和新内容哈希；不提供静默兼容转换。
- 示例和服务器私有草稿需要显式迁移、重新登记并消耗新的研究族名额。

## 12. 验收标准

- 编译或结构失败的测试证明结果数据源打开次数为零。
- 冒烟测试证明结果数据源永远未构造或读取。
- 可见验证使用冒烟测试路径、伪清单哈希、错误 Git 或配置哈希时失败。
- 需要 120 个观察值的因子获得至少 120 个真实交易观察用于预热；不足时失败。
- 人工注入依赖未来的编译计划会被动态探针拒绝。
- 声明参考池但不提供参考因子时失败；结构或输出重复不能通过。
- 写入 `visible_passed` 之前，最终运行目录已存在且全部产物哈希可重算。
- 模拟 SIGTERM 或遗留暂存目录后产生 `interrupted`，账本仍可校验。
- 全部 Mac 测试只使用程序生成合成数据；真实验收只在公司 Linux 服务器运行。

## 13. 后续版本边界

V0.2 才增加轨迹、结构化引用、归因和三类记忆；V1 先用人工或录制输入跑通可重放的 RMA→宏观研究→微观研究→评价→交叉审查；V1.1 才接入在线大语言模型；密封样本外检验、机制验证、组合构建、CPCV、Deflated Sharpe Ratio、成本容量和签名发布属于 V2。CPCV 只能检验开发阶段不同路径下的稳定性，不能替代最终密封样本外检验；Deflated Sharpe Ratio 只评价完整策略收益，不能替代因子 RankIC 的 HAC 与多重检验。TreeSHAP 只能作为开发区间的模型筛选诊断，不能成为认证证据。
