# 米筐 Barra 风险数据运行手册

## 研究边界

本数据用于解释候选组合相对沪深 300 的行业与风格暴露、已实现因子收益贡献，以及因子协方差和特异风险构成。归因结果不等于因子具有 Alpha，也不构成因果机制验证或实盘结论。

完整风险分解只覆盖 `2012-01-01` 至 `2026-06-30`。米筐在 2010 年及部分 2011 年日期没有返回可用的 `specific_risk`，因此这些年份不得标记为完整风险分解。

## 数据身份

- 来源：RiceQuant RQData
- 模型：`v2trd`
- 行业分类：`sws_2021`
- 因子收益：`implicit`
- 基准：`000300.XSHG`
- 风险频率：日频
- 服务器派生根目录：`/data/factor_miner_derived/barra/rqdata_v2trd_sws2021`

程序调用 `get_factor_exposure`、`get_factor_return`、`get_specific_return`、`get_factor_covariance`、`get_specific_risk` 和沪深 300 历史权重接口。凭据只从服务器私有环境文件读取，不进入 Git、清单或日志。

## 目录合同

数据按数据集优先、年度分片方式保存：

```text
rqdata_v2trd_sws2021/
├── exposure/year=YYYY.parquet
├── factor_return/year=YYYY.parquet
├── specific_return/year=YYYY.parquet
├── factor_covariance/year=YYYY.parquet
├── specific_risk/year=YYYY.parquet
├── csi300_weight/year=YYYY.parquet
├── partitions/year=YYYY.json
├── factor_catalog.json
└── manifest.json
```

年度文件全部写完并核验后才发布对应的 `partitions/year=YYYY.json`。重复运行会复用已完整提交的年份；文件缺失或哈希不一致时硬失败。

数据库字段和 Parquet 字段使用英文。行业因子稳定映射为 `industry_001` 等代码，风格因子使用 `Size` 和 `Style_*`；中文名称与描述只保存在 `factor_catalog.json`。

## 抓取命令

以下命令只允许在已配置合法米筐凭据和私有数据目录的受控环境执行：

```bash
uv run factor-miner barra fetch-ricequant \
  --env-file /data/factor_miner_artifacts/private_config/rqdata.env \
  --universe-uri /data/quantlake/raw/security_flags/daily_security_flags.parquet \
  --derived-root /data/factor_miner_derived/barra/rqdata_v2trd_sws2021 \
  --start 2012-01-01 \
  --end 2026-06-30 \
  --batch-size 300
```

## 归因口径

- 暴露按证券在信号日或此前最近可得日向后对齐，最大陈旧期为 7 天，禁止使用未来暴露。
- 沪深 300 权重按全局历史快照选择，避免退市或退出成分被逐证券向后填充而复活。
- 日频因子收益按每个持有窗口的 `[entry_date, exit_date)` 复合为持有期收益。
- 因子风险为主动因子暴露向量与协方差矩阵的二次型。
- 特异风险为每只证券特异波动率平方后，按主动权重平方加总。
- 总风险等于因子方差与特异方差之和；任何缺失、非有限、非对称或负风险输入都会硬失败。
