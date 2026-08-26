# 阶段 A 固定候选 Pilot 真实运行

## 研究边界

本 runbook 只适用于三个已经冻结的人工候选。运行结果只能称为“通过冻结可见协议计算
的候选因子诊断”，不能称为有效 Alpha、认证因子或生产结论。所有真实因子、IC、组合和
Barra 计算必须在公司 Linux 服务器执行；Mac 只能做合成测试、代码审查和 SSH 远程控制。

阶段 A 不调用 DeepSeek，不读取或写入 120 槽研究族、generation seal 或污染的
`llmfamily_1bae19965638a6ac9620e0b0` 账本。`/data/quantlake` 只读，运行产物必须写入单独
配置的产物根目录。

## 当前预检记录

| 项目 | 记录 |
| --- | --- |
| 固定候选 | `tests/fixtures/pilot/fixed_candidates.json`，恰好 `pilot_fixed_001` 至 `pilot_fixed_003` |
| CSI300 固定数据 | `/data/factor_miner_derived/csi300/`；`index_code=000300.XSHG`；指数日线 4,002 条、历史成分 1,200,600 条、800 只成分股；固定数据 `auto_update_managed=false` |
| CSI300 截止日 | 2026-06-30；指数日线、历史成分和历史成分股后复权日线均为独立固定输入 |
| QuantLake release | `quantlake-20260803-dfabe97af524106f`；行情与 L2 cutoff 为 2026-08-03；`l2_state_v1` |
| 交易日历 | `/data/factor_miner_derived/trading_calendar/trading_calendar_20100101_20260630.parquet`；RiceQuant `get_trading_dates`；6,025 行、4,002 个开放日、2,023 个关闭日；Parquet SHA-256=`6c0cd09e1b8f0233f682b2503d627bf8c8251905fdda1e3a593f4f428278e747` |
| 历史成分 | `/data/factor_miner_derived/csi300/components_daily.parquet`；仅保留 `000300.XSHG`；输入哈希=`3cffe322630226eaca9ff910b8c2fb07648f79038b9ee720c8c8e50a8129d68` |
| 因子字段证明 | `/data/factor_miner_derived/csi300/pilot_market_field_registry.csv`；`close->adj_close`、`volume->volume`；SHA-256=`7806886a8db52d6618c9e9df62ab4f26143678a8d446ddf958a5dd13931183fb` |
| 持有期开盘价 | 使用 QuantLake `processed/adjusted_bar` 的服务器 `adj_open`；CSI300 `stock_daily` 只覆盖成分存续日，不能覆盖成分退出后的持有期退出价，因此本次不把它作为持有期开盘价源，也不做静默补值 |
| Barra | 已发现服务器风格暴露目录和因子收益元数据；缺行业、基准权重或身份时必须写 `not_available` |
| 自动更新 | 未修改 QuantLake 脚本、systemd、cron 或 `/data/csi300/` 内容 |
| 真实运行 | `run_907e975eec05a09f4db07219` 已发布并核验；清单 SHA-256=`26bfb9088404550d050e6ef8b14eeaa97e3d461b3533cfaf866c0fd78aea0008`；PostgreSQL 投影快照 SHA-256=`53fe5322aaccacb4f20e0a011fa6cd1e60e354e49684c0c7a3a931b107cd45eb` |

## 当前结论

交易日历和 CSI300 基准均已完成服务器合同核验，真实 Pilot 已在 Linux 完成。三候选均
有 IC/RankIC 诊断、周二十组组合和真实交易日日历指标；Barra 三个候选均明确记录为
`not_available`，没有伪造归因或超额收益。结果只表示通过冻结可见协议的候选因子诊断，
不表示 Alpha、认证因子或生产结论。

本次 run 的输入清单哈希为
`6c025ae0a651e81fca8c74feb07a4cfb95458ede1f4125066113e96bf50af672`，配置哈希为
`1ae1b608ba5ac88d32f7ef0bfa7d72af7efc111c8c2c7d8a1d4e38c99332bd52`。Dashboard PostgreSQL
读模型已成功幂等投影，写入 10 个产物引用、3 个候选摘要、75 个组合指标和 63,825 条
组合日收益；本地产物仍是正式主存储。

## 解锁后的执行顺序

若重新运行或扩大可见区间，仍必须先完成独立交易日历、CSI300 基准、历史成分和价格源
的 SHA-256、版本、开放日类型、主键唯一性、覆盖范围和截止日核验，再按以下顺序执行：

1. 在服务器只读预检 QuantLake release、状态表、固定 CSI300、交易日历和可选 Barra，生成
   完整 `PilotInputManifest`。
2. 冻结 `PilotRunRequest`、评价政策、组合政策、代码提交和配置哈希；确认可见区间不超过
   各输入截止日。
3. 使用三个固定候选计算原始因子、开盘到开盘 IC/RankIC、周二十组组合、Q10-Q1、Q9-Q2、
   双边 14bp 成本和真实日历年化指标。
4. Barra 全部输入满足合同时写归因；否则对每个候选写 `status=not_available` 和具体缺失
   输入。Barra 不得阻塞前述主链路。
5. 先原子发布独立产物根目录下的 `run_manifest.json` 及其引用文件，再用
   `factor-miner pilot project <pilot_run_id> --artifact-root ... --dsn ...` 做数据库投影。
   数据库失败时保留本地产物，恢复后重试投影。
6. 使用 `verify_published_run` 核对全部引用哈希，再打开只读 Dashboard；确认三候选无论
   指标好坏都保留完整诊断。

## 完成判定

阶段 A 的真实 Linux 产物、清单核验和 PostgreSQL Dashboard 投影已完成；Barra 保持合同
要求的 `not_available`，不构成阶段 A 阻塞项。结果仍只能称为候选因子诊断。
