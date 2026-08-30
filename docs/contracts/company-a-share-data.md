# A 股数据适配说明

本文件是[标准面板数据合同](标准面板数据合同.md)的 A 股扩展，不是系统唯一数据入口。QuantLake、Linux 服务器和 SSH 均不是核心运行前提。

A 股状态表除统一三类 mask 外，可以提供以下 Boolean 列：`is_st`、`is_newly_listed`、`is_suspended`、`can_buy`、`can_sell`。一旦提供其中任一列，就必须完整提供五列，系统会复核：

- `valid_for_factor_compute = NOT (is_st OR is_newly_listed)`；
- `valid_for_factor_rank = valid_for_factor_compute AND NOT is_suspended`；
- `valid_for_trading = valid_for_factor_compute AND can_buy AND can_sell`。

涨跌停、停牌、上市状态、退市和公司行动必须来自可追溯的数据发布。前复权、后复权或不复权都可以接入，但必须在 manifest 中明确记录并在同一研究批次内保持一致。

若使用 QuantLake，应将其视为只读上游，并把标准化三表写入独立发布目录；运行产物还要写入另一个独立目录。核心包不得运行时 import `huan_quant` 或解析个人目录。
