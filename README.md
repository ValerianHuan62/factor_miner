# Factor Miner：可审计的 LLM 因子研究框架

Factor Miner 是一套独立 Python 项目，用于把人工或 LLM 生成的金融假设转换为类型化因子表达式，并在结果揭晓前冻结研究协议，完成因子计算、RankIC/HAC 检验、多重检验、冗余检查、组合诊断、不可变账本和中文 Dashboard 展示。

它不是“让模型自由写 Python”的因子生成器。候选只能使用白名单类型化 AST；行情、标签、统计结果和密钥不会进入 LLM 请求。

## 项目亮点

- 类型化因子 DSL，禁止 `eval`、`exec`、动态导入和任意 Python 表达式。
- 因子主张、方向、机制、证伪路径和试验预算在读取结果前冻结。
- RankIC、HAC t 值、Bonferroni、多重检验族和冗余检查均可审计。
- 120 槽正式研究族、轻量演化、市场状态、覆盖图谱和组合诊断模块。
- JSON/JSONL 不可变主账本；PostgreSQL 仅作为可重建 Dashboard 读模型。
- DeepSeek 只接收脱敏公开文本、字段白名单、操作符和 Schema。
- 完整合成测试不读取真实行情、QuantLake、个人文件或 API Token。

## 运行环境

- Python：3.12
- 包管理：`uv`
- 原生开发与合成测试：macOS 或 Linux
- 真实数据计算：授权的 Linux 研究环境
- 可选服务：PostgreSQL、Streamlit Dashboard
- 数据输入：满足 [A 股数据合同](docs/contracts/company-a-share-data.md) 的只读 QuantLake 或等价发布

## 五分钟开始

安装 [uv](https://docs.astral.sh/uv/) 后执行：

```bash
git clone https://github.com/ValerianHuan62/factor_miner.git
cd factor_miner
cp configs/company_a_share.env.example .env
make install
make test
```

本机验证过的命令：

```bash
uv sync --frozen --extra dashboard
uv run python -m unittest discover -s tests
```

2026-08-26 在 Apple Silicon Mac、Python 3.12 上运行 707 项合成测试，全部通过。测试输出中的 Polars sortedness 与 HMM convergence 信息为警告，不影响测试结论。

## 常用命令

```bash
# 查看 CLI
uv run factor-miner --help

# 校验与编译示例候选
uv run factor-miner validate-spec examples/candidates/momentum_20d.json
uv run factor-miner compile-spec examples/candidates/momentum_20d.json

# 运行最小合成端到端测试
uv run python -m unittest tests.test_synthetic_e2e -v

# 启动中文 Dashboard；需要先配置 PostgreSQL 读模型
make dashboard
```

真实研究不会仅凭一个 Token 自动开始。除了 `DEEPSEEK_API_KEY`，还必须显式提供只读数据发布、清单哈希、截止日期、状态表、产物目录、研究配置和 PostgreSQL DSN；缺失时系统按设计硬失败。这样可避免把错误数据或未来信息悄悄当成研究结果。

## 目录结构

```text
factor_miner/
├── src/factor_miner/    # 核心 DSL、计算、评价、LLM、研究编排和账本
├── dashboard/           # Streamlit 中文研究 Dashboard
├── configs/             # 无密钥环境变量模板
├── examples/            # 合成候选、研究族、策略和参考库示例
├── deploy/              # 通用 Linux systemd 模板
├── docs/                # 数据合同、治理约束、设计和运行手册
├── tests/               # 707 项纯合成/合同测试
├── pyproject.toml       # Python 依赖
└── uv.lock              # 冻结依赖
```

## 研究结论边界

系统输出只能称为“通过当前协议验证的候选因子”。可见样本通过不等于密封 OOS 通过，相关性不等于因果机制，统计显著不等于扣除成本后可交易。失败、中断、重复候选和 DSL 错误都保留在原多重检验族中，不会从分母消失。

## 数据与密钥

仓库只保存代码、测试、合同、示例和配置模板。以下内容已由 `.gitignore` 排除：

- `.env`、Token、数据库密码和本机配置
- Parquet/Arrow/CSV、模型、真实候选和运行账本
- 研究产物、回测结果、日志和数据库备份

提交 GitHub 前请执行 [发布前检查清单](docs/GITHUB发布前检查清单.md)，并确认你拥有公开代码与文档的权利。仓库目前没有附加开源许可证；公开发布前应根据你的授权范围选择许可证或保持私有。

## 深入阅读

- [从这里开始](docs/START_HERE.md)
- [研究治理约束](docs/constraints/RESEARCH_GOVERNANCE.md)
- [因子研究协议](docs/constraints/FACTOR_RESEARCH_PROTOCOL.md)
- [A 股数据合同](docs/contracts/company-a-share-data.md)
- [Dashboard 自主研究运行手册](docs/runbooks/Dashboard自主研究运行.md)
- [本地开发与远程服务器运行](docs/本地开发与远程服务器运行.md)
