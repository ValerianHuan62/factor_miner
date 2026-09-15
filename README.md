# Factor Miner

Factor Miner 是一个面向 A 股和美股的量化因子研究系统。它将论文或经济学线索转化为可复现的研究流程：登记假设、生成受限公式、在真实数据上计算、验证预测与交易表现、检查冗余，并把审核后的因子交付给下游模型研究。

项目的目标是提供可追溯的研究候选与诊断证据，不把任何单一回测或审核结果直接表述为实盘 Alpha 或投资建议。

## 功能

- **假设驱动的研究登记**：在评价前冻结论文来源、经济机制、方向、代理、竞争解释、证伪路径、数据口径和研究预算。
- **假设—公式一致性验证**：参考 FaVOR 的构念验证思想，把事前经济主张拆成可观测状态、方向和五分位响应；公式必须先通过不读取收益标签的构念检验，才能进入预测评价。
- **安全的因子表达式**：使用类型化 AST 与白名单算子确定性编译公式，拒绝未来函数、标签泄漏、任意 Python 和未声明的数据字段。
- **可复现的真实数据验证**：在版本化标准面板、交易日历和状态 mask 上计算原始因子，并保存数据、代码和配置身份。
- **统计与交易诊断**：分别计算 IC、RankIC、HAC 显著性、多重检验结果、分组收益、成交约束、成本与风险指标。
- **因子冗余与代表库**：先做结构查重，再做逐日截面相关和联合贡献诊断；审核后保留研究代表与同类替补。
- **结果交付与界面**：JSON/JSONL 和运行产物作为事实源，PostgreSQL 提供可重建读模型，Dashboard 用于查看结果、代表库和研究任务。

## 研究流程

```mermaid
flowchart LR
    A[论文与经济学线索] --> B[可证伪假设与冻结 Gamma]
    B --> C[类型化公式]
    C --> D[FaVOR 构念一致性验证]
    D --> E[Quality：IC、稳定性与成本]
    E --> F[Correlation/MFCF：数值冗余]
    F --> G[SDF-inspired：联合条件贡献]
    G --> H[SPPC-inspired：净 Sharpe 经济贡献]
    H --> I[审核代表库与原始特征交付]
```

### 从公式忠实性到经济冗余的四层筛选

假设不是只生成一句叙事。每个 FaVOR 候选都先冻结 `Gamma`：经济主张对应哪些可观测量、预期单调方向、覆盖要求和允许的公式语义。系统在不读取未来收益标签的条件下，对因子五分位与这些事前测量做覆盖、中心趋势、尾部变化、统计一致性和方向一致性检查。这样验证的是**公式是否测到了它声称在测的对象**；它不能替代经济机制的独立因果证明。

构念通过后，因子筛选遵循下列顺序：

| 阶段 | 回答的问题 | 当前实现 |
| --- | --- | --- |
| **Quality** | 单个因子本身是否有稳定、可交易的预测信息？ | 冻结口径下的 IC、RankIC、HAC、多重检验、覆盖、分组收益、成本与回撤诊断。 |
| **Correlation / MFCF** | 它是否只是数值上重复的信号？ | 先做公式哈希查重；再计算共同截面上的 Pearson 与 Spearman。MFCF 使用 `max(|Pearson|, |Spearman|)` 的 complete-linkage 聚类，优先移除明显同质的候选。 |
| **SDF-inspired Conditional Contribution** | 在其他因子已经存在时，它是否还有条件增量信息？ | 对通过相关过滤的因子，在固定训练、标签 purge、交易和成本口径下联合滚动拟合 Ridge；保存联合权重，并执行 leave-one-factor-out 重训，比较全模型与删除该因子后的 OOS `ΔSharpe`。单因子 IC 高但 `ΔSharpe≈0` 的因子，说明其信息可被现有池替代。 |
| **SPPC-inspired Economic Contribution** | 它是否真的给可交易组合增加经济价值？ | 以统一因果成交后的参考净 Sharpe 作为价值函数，在冻结子集上做 Shapley 归因；当前实现按事前机制组计算 Shapley，并同时保留逐因子删除损失。它衡量的是经济增量，不是固定模型里的预测重要性。 |

因此，项目的核心筛选逻辑是：

> **公式忠实性 → Quality → Correlation/MFCF → SDF-inspired Conditional Contribution → SPPC-inspired Economic Contribution**

其中 Pearson/Spearman 只处理数值相似性，不能证明独立预测价值；SDF 式删除重训回答条件增量，SPPC 式 Shapley 才把问题落到“该因子（或机制组）为可交易组合贡献了多少净 Sharpe”。当前实现借鉴 SDF 与 SPPC 的研究思想：普通收益预测 Ridge 不等同于论文中的 Ridge-SDF，机制组 Shapley 也不宣称是 SPPC 的完整复现。

原始因子始终保留原方向和缺失值，不混入标准化、标签或模型预处理。失败、重复和中断记录不会被删除；候选资格、审核采纳和独立统计确认分别保存。

## 当前研究状态

系统当前保留 A 股和美股共用的研究流程，但默认暂停新增因子探索，重点使用既有代表库开展策略与模型研究。

已完成的 A 股联合冗余诊断从 51 个既有候选中审核采纳了 36 个研究代表，并保留 15 个同类替补。研究代表可以作为后续研究输入，但采纳不改写原候选的独立统计资格；未完成独立经济机制检验的候选仍标记为 `mechanism_unverified`。

## Dashboard 与 API 研究

Dashboard 将候选账本、验证指标、回测报告和代表库组织为可检索界面：

- **看结果**：候选指标、成本与回测诊断、审核状态和详情。
- **研究代表库**：按市场展示已采纳代表和同类替补，审核状态与独立统计确认分别显示。
- **API 研究**：用户配置本机 DeepSeek API Key、导入完整计划并显式启动后，页面在后台调用 `run-favor-api`。生成公式仍进入 `register-favor`、`submit-favor` 和 `run-favor` 的正式验证链路。

API 仅发送已授权的公开研究假设与测量合同；私有行情、完整研究结果和 API Key 不进入 Git。具体使用方式见 [Dashboard 的 API 研究与审核状态](docs/runbooks/Dashboard的API研究与审核状态.md)。

## 项目结构

```text
src/factor_miner/            研究合同、DSL、计算、统计、冗余和 CLI
src/factor_miner_pg/         PostgreSQL 可重建读模型
dashboard/                   Streamlit 结果与研究操作界面
docs/contracts/              数据、统计、预算和跨市场研究合同
docs/runbooks/               本地运行、Dashboard 与特征交付说明
tests/                       合成数据和合同回归测试
```

项目使用 Python 3.12、Polars、Pydantic、Typer、Streamlit 和 PostgreSQL。JSON/JSONL 与不可变运行产物是研究事实源；PostgreSQL 仅承担可重建查询和 Dashboard 展示。核心包不依赖个人数据目录、个人数据库或下游模型项目。

审核后的代表可通过 `export-joint-features` 导出为版本化原始 Parquet 宽表及来源清单，供下游构建 Dataset 和训练模型。宽表不含标签、标准化或模型预处理，详见 [因子宽表导出与模型接入](docs/runbooks/因子宽表导出与模型接入.md)。

## 快速开始

需要 Python 3.12 和 `uv`：

```bash
git clone https://github.com/ValerianHuan62/factor_miner.git
cd factor_miner
uv sync --frozen

# 公开合成样例：不会启动真实研究
uv run factor-miner validate-spec examples/candidates/momentum_20d.json
uv run factor-miner compile-spec examples/candidates/momentum_20d.json
uv run python -m unittest tests.test_synthetic_e2e -v

# 常用入口
make cli
make test
make dashboard
```

真实研究需要版本化 CSV/Parquet 面板、交易日历、状态 mask、复权口径和独立产物目录。仓库不包含私有行情、真实候选、完整研究产物、模型或密钥。运行细节见 [本地与服务器研究运行](docs/runbooks/本地与服务器研究运行.md)。

## 文档

- [完整流程与日常使用](docs/当前流程与日常使用.md)：端到端研究流程与日常入口。
- [系统架构](ARCHITECTURE.md)：模块职责、数据流和存储边界。
- [Dashboard 的 API 研究与审核状态](docs/runbooks/Dashboard的API研究与审核状态.md)：网页研究任务、密钥、状态和运维说明。
- [因子宽表导出与模型接入](docs/runbooks/因子宽表导出与模型接入.md)：审核代表的版本化特征交付合同。
- [项目面试讲解](docs/项目面试讲解.md)：独立的面试介绍、常见追问和演示顺序。
- [文档索引](docs/README.md)：全部研究合同、运行手册和历史设计。

项目采用 [MIT 许可证](LICENSE)。
