# V1 服务器 Dashboard 部署合同

## 金融研究边界

Dashboard 只展示已经由服务器冻结协议计算并发布的候选诊断。它不改变假设、候选状态、评价政策、统计预算或账本，也不把“诊断计算成功”改写成“有效 Alpha”。只有后续明确通过质量闸门的候选，才能进入“通过检验因子”目录。

真实行情、逐股票面板、完整失败账本和密钥只留在公司 Linux 服务器。Mac 通过 SSH 隧道查看服务器 Dashboard 或使用只读数据库客户端，不承担计算职责。

## 服务器组件

核心 `factor_miner` 不依赖数据库或 Web 框架。服务器部署额外安装 Dashboard 依赖：

| 组件 | 固定范围 | 官方来源与许可证 | 维护状态记录 |
|---|---|---|---|
| `psycopg[binary]` | `>=3.3.4,<4` | [PyPI psycopg](https://pypi.org/project/psycopg/)，LGPL-3.0-only | PyPI 标记 Production/Stable；Psycopg 3 为当前主版本 |
| `streamlit` | `>=1.59.2,<2` | [PyPI Streamlit](https://pypi.org/project/streamlit/)，Apache-2.0 | PyPI 标记 Production/Stable |
| `plotly` | `>=6.9.0,<7` | [PyPI Plotly](https://pypi.org/project/plotly/)，MIT | PyPI 标记 Production/Stable |

版本、锁文件和许可证变更必须重新审核，不能在服务器上临时安装未登记版本。

## PostgreSQL 边界

数据库名建议为 `factor_miner_dashboard`。它是从不可变运行产物重建的读模型，不替代 JSON/JSONL/Parquet 正式主存储。

数据库只投影：候选 ID 与 Spec 哈希、质量状态、组合指标、逐日组合汇总、IC/RankIC 摘要、Barra 暴露与已实现收益归因、产物路径和哈希。原始行情、逐股票因子值、密钥、完整请求/响应正文和失败槽位账本不进入读库。

应用使用独立写入账号执行幂等投影；日常查看使用只读账号。数据库端口不直接暴露公网。

## 服务器运行方式

在服务器私有环境中：

```bash
uv sync --extra dashboard
psql "$FM_DASHBOARD_DSN" -f dashboard/migrations/001_initial.sql
export FM_DASHBOARD_DSN='服务器私有环境提供的连接串'
export FM_DASHBOARD_RUN_ID='run_<24位十六进制>'
python -m dashboard.project "$FM_DASHBOARD_RUN_ID" \
  --artifact-root /data/factor_miner_artifacts
streamlit run dashboard/app.py --server.address 127.0.0.1 --server.port 8501
```

投影前必须先核验运行清单哈希，再调用 `project_run_artifacts`；Dashboard 不能直接读取 QuantLake，也不能绕过 `verify_published_run`。

Mac 查看时使用 SSH 隧道：

```bash
ssh -L 18501:127.0.0.1:8501 <user>@<server>
```

然后在 Mac 浏览器打开 `http://127.0.0.1:18501`。Mac 端 PostgreSQL 客户端也应通过 SSH 隧道连接服务器数据库，并使用只读账号。

## 当前验收状态

历史服务器源码目录中的未提交改动已在部署前审查并固化为 V0.5
修正并保持原样，没有覆盖。V1 代码部署在独立目录
独立部署快照中，Linux 合成回归为 349 项通过（跳过 1 项仅限 Mac
边界测试）。服务器 PostgreSQL 16 已启动，独立数据库
`factor_miner_dashboard` 已创建并完成迁移，私有 DSN 文件位于
`/data/factor_miner_artifacts/dashboard/postgres.env`。

仍未完成的是 Streamlit/Plotly 依赖与只读服务验收、完整两条 LLM arm 的真实批准批次、
真实组合/Barra 数据端口和质量闸门。Codex 没有读取 QuantLake 原始数据，也没有代发
外部 DeepSeek 请求；在这些条件完成前不得声称真实 120 槽回测已经完成。
