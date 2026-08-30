# Factor Miner：可审计的因子研究系统

Factor Miner 把人工或受控 LLM 提出的金融假设，转换为可审计、可复现的候选因子研究流程。核心链路覆盖因子图谱、假设覆盖分析、研究记忆、自进化候选生成、类型化 DSL、真实数据计算、统计检验、组合诊断、不可变账本和中文 Dashboard。

项目不会让模型自由编写 Python，也不会把行情、标签、统计结果或密钥发送给模型。所有输出只能称为“通过当前协议验证的候选因子”，不能直接视为 Alpha、生产结论或实盘建议。

## 核心能力

- **因子图谱**：同时刻画公式结构和实际信号相似性，识别重复簇与研究空白。
- **假设覆盖图谱**：把覆盖缺口整理为脱敏中文简报，指导模型提出可证伪的新假设。
- **研究记忆与自进化**：保留成功、失败、拒绝和未执行记录，为下一轮研究提供有边界的上下文。
- **安全因子 DSL**：只允许白名单类型化 AST，禁止 `eval`、`exec`、动态导入和未来信息。
- **冻结研究协议**：结果揭晓前固定主张、方向、股票池、标签、预算、HAC 参数和多重检验范围。
- **可审计评价**：提供 RankIC、HAC t 值、Bonferroni、增量信息、冗余、组合和稳健性诊断。
- **不可变主存储**：JSON/JSONL 与运行产物保存事实；PostgreSQL 只承载可重建的 Dashboard 读模型。

## 快速开始

完整系统需要 Python 3.12、`uv`、PostgreSQL 和 Streamlit Dashboard。PostgreSQL 是可重建读模型，JSON/JSONL 与运行产物仍是研究事实源；数据库不可用时不得假装研究已经完整发布。

```bash
git clone https://github.com/ValerianHuan62/factor_miner.git
cd factor_miner
uv sync --frozen
export FM_DASHBOARD_DSN='postgresql://factor_miner:factor_miner_local@127.0.0.1:5432/factor_miner'
uv run factor-miner validate-spec examples/candidates/momentum_20d.json
uv run factor-miner compile-spec examples/candidates/momentum_20d.json
uv run python -m unittest tests.test_synthetic_e2e -v
```

以上命令会校验并编译一个合成候选，再运行最小端到端测试。仓库同时提供 `make demo` 快捷命令。查看完整 CLI：

```bash
make cli
```

常用开发命令：

```bash
make test                 # 完整合成与合同测试
make dashboard            # 启动本地 Dashboard，需要 PostgreSQL DSN
```

如果本机没有 PostgreSQL，可用仓库提供的容器配置启动一个本地实例：

```bash
docker compose up -d postgres
```

## 使用门槛

| 能力 | 硬性要求 |
| --- | --- |
| 安装与合成测试 | Python 3.12、`uv`；测试不读取真实数据或密钥 |
| 完整系统 | PostgreSQL DSN、Streamlit Dashboard；依赖已包含在默认安装中 |
| 受控 LLM 假设生成 | API Key、脱敏请求、精确请求哈希授权和人工审批 |
| 真实跨市场研究 | 标准面板发布、状态 mask、交易日历、标签、清单哈希、截止日和独立产物目录 |

真实研究可在本地电脑或服务器运行，不要求 SSH、Linux、QuantLake 或个人数据库。A 股、美股和其他市场都通过同一标准面板合同接入；QuantLake 只是可选的 A 股上游。缺少数据身份、字段、状态 mask、配置哈希或截止日一致性时，系统按设计硬失败。

## 接入自己的数据

输入 CSV 或 Parquet 至少包含 `date, asset, open, high, low, close, volume`。若数据已处理交易状态，可额外提供 `valid_for_factor_compute`、`valid_for_factor_rank`、`valid_for_trading` 三个 Boolean mask；否则必须显式确认全部记录可用于演示：

```bash
uv run factor-miner data prepare-local examples/data/us_equities_sample.csv \
  --output-root .local/releases/us-sample \
  --artifact-root .local/artifacts \
  --adjustment-convention split_adjusted \
  --calendar-version us-sample-v1 \
  --assume-tradable
```

命令会生成标准 Parquet 三表、内容哈希清单和 `runtime.env`。正式研究应由数据适配器提供真实的停牌、退市、可交易状态，而不是使用 `--assume-tradable`。

## 项目结构

```text
factor_miner/
├── src/factor_miner/    # DSL、计算、评价、图谱、LLM 编排、研究记忆与账本
├── dashboard/           # Streamlit 中文只读 Dashboard 与受控研究台
├── configs/             # 无密钥配置模板
├── examples/            # 可公开运行的合成输入
├── deploy/              # 可选的 Linux systemd 部署模板
├── docs/                # 研究协议、数据合同和运行手册
├── tests/               # 纯合成与合同回归测试
├── ARCHITECTURE.md      # 成品架构与数据流
├── pyproject.toml       # 包元数据与依赖
└── uv.lock              # 冻结依赖
```

## 研究边界

- 候选必须在读取结果前冻结金融主张、预期方向、机制、代理、竞争解释、失效方式和证伪路径。
- 原始因子、预处理、标签、评价和组合构建严格分层；基本面按可得日对齐。
- 失败、中断、重复和表达式错误仍占用预登记试验名额，不会从多重检验分母中消失。
- 相关性不等于因果机制，可见区间通过不等于密封样本外通过，统计显著不等于扣除成本后可交易。
- 真实数据、运行配置、账本、模型、密钥和服务器路径解析结果不得进入 Git。

## 文档

- [系统架构](ARCHITECTURE.md)
- [因子研究协议](docs/constraints/FACTOR_RESEARCH_PROTOCOL.md)
- [研究治理](docs/constraints/RESEARCH_GOVERNANCE.md)
- [标准面板数据合同](docs/contracts/标准面板数据合同.md)
- [本地与服务器研究运行](docs/runbooks/本地与服务器研究运行.md)
- [Dashboard 自主研究](docs/runbooks/Dashboard自主研究运行.md)

## 许可证

本项目采用 [MIT License](LICENSE)。
