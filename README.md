# Factor Miner

Factor Miner 是一个面向 A 股和美股的量化因子研究系统。它将论文或经济学线索转化为可复现的研究流程：登记假设、生成受限公式、在真实数据上计算、验证预测与交易表现、检查冗余，并把审核后的因子交付给下游模型研究。

项目的目标是提供可追溯的研究候选与诊断证据，不把任何单一回测或审核结果直接表述为实盘 Alpha 或投资建议。

## 功能

- **假设驱动的研究登记**：在评价前冻结论文来源、经济机制、方向、代理、竞争解释、证伪路径、数据口径和研究预算。
- **安全的因子表达式**：使用类型化 AST 与白名单算子确定性编译公式，拒绝未来函数、标签泄漏、任意 Python 和未声明的数据字段。
- **可复现的真实数据验证**：在版本化标准面板、交易日历和状态 mask 上计算原始因子，并保存数据、代码和配置身份。
- **统计与交易诊断**：分别计算 IC、RankIC、HAC 显著性、多重检验结果、分组收益、成交约束、成本与风险指标。
- **因子冗余与代表库**：先做结构查重，再做逐日截面相关和联合贡献诊断；审核后保留研究代表与同类替补。
- **结果交付与界面**：JSON/JSONL 和运行产物作为事实源，PostgreSQL 提供可重建读模型，Dashboard 用于查看结果、代表库和研究任务。

## 研究流程

```mermaid
flowchart LR
    A[论文与经济学线索] --> B[可证伪假设]
    B --> C[冻结数据、样本、成本和预算]
    C --> D[类型化公式与构念校验]
    D --> E[真实面板上的原始因子计算]
    E --> F[IC、RankIC 与统计检验]
    F --> G[因果成交与成本回测]
    G --> H[冗余与联合贡献诊断]
    H --> I[审核代表库与结果发布]
    I --> J[原始因子宽表供下游模型使用]
```

每个阶段都有明确边界：

1. **假设与可行性**：先确认研究问题能被现有字段与可得时间测量，再登记候选名额。
2. **公式与原始计算**：公式只使用白名单 AST；原始因子保留原方向和缺失值，不混入标准化、标签或模型预处理。
3. **预测与交易验证**：预测信息和交易表现分开评价；排序使用信号日信息，成交与终值按冻结规则处理。
4. **冗余与采纳**：公式哈希、输出相关和联合诊断分别回答结构重复、样本重复和组合增量问题。
5. **发布与复现**：失败、重复和中断记录不会被删除；候选资格、审核采纳和独立统计确认分别保存。

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
