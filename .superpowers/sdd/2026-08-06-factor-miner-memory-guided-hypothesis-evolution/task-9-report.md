# Task 9 合成端到端验证报告

## 研究边界

本次验证只使用脱敏 coverage graph、固定合成响应和程序生成的状态对象。测试没有读取 A 股行情，没有计算真实候选、IC、冗余或回测，因此不能推出生产因子、有效 Alpha 或任何真实市场结论。

## 已覆盖合同

- coverage graph loader、三类 gap report 和脱敏外发请求；
- 十个逻辑草案与逐条人工批准；只批准九个时在表达式、回测和发布之前停止；
- 三设计 window-only 失败闸门；四 arm、120 槽、`DESIGN_DIVERSITY_FAILED`、`cross_arm_redundant` 和 `not_executed` 记录；
- Linux-only strict generation seal、120 条脱敏 memory entries 和 fake Dashboard snapshot；
- 下一轮 snapshot 不引用本轮条目；污染 family 不进入快照、请求或 Dashboard payload；
- 请求与数据库式投影 payload 不含原始数据、模型原文、密钥或服务器路径。

## 复核边界

公司 Linux 真实数据合同、`/data/quantlake` 只读性、正式产物根、真实发布账本和真实 IC/冗余/回测未在本机执行，仅保留为合同复核事项。没有进行新的十假设人工批准，也没有生成生产因子。

## 测试记录

- 新增合成闭环：`.venv/bin/python -m unittest tests.test_research_evolution_synthetic_e2e`，2 项通过。
- Task 6/7/8 相关组合：58 项通过。
- 完整 discover：运行 500 项，499 项通过，1 项失败。失败是既有 `test_import_boundary` 命中 `src/factor_miner/research_gap.py` 中原有的 `/Users/` 脱敏正则；本 Task 9 未修改产品代码，未对该既有缺陷做修复。
- 用户指定的 `python -m unittest ...` 无法启动，精确原因是 `zsh:1: command not found: python`；使用项目 `.venv/bin/python` 完成替代验证。
- `git diff --check`：通过。
