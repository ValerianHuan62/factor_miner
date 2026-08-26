# Task7 完成报告

## 研究边界

本任务只实现已封存研究族评价结果的不可变发布、脱敏研究记忆和 Dashboard 只读投影，不执行真实 A 股计算、IC、回测或机制认证。120 个预登记槽位中的失败、重复和未执行状态均保留；因此任何投影都只能说明研究流程状态和可审计身份，不能被解释为有效 Alpha、证据或生产结论。数据库断开不改变 JSON/JSONL 正式主存储。

## 实现结果

- `publish_campaign_evaluation` 先复核 generation seal，再一次生成带 UTC 时区的 `published_at` 并写入 `run/input_manifest.json`。
- 发布成功后追加完整 120 条脱敏 memory entries，冻结 snapshot/reference；条目只保留身份、状态、有限摘要和哈希，不复制原始行情、逐日指标或模型原文。
- 污染 family 在记忆构造入口硬拒绝；同一记忆对象重复发布幂等，部分重复或身份冲突失败。
- Dashboard 读模型兼容没有演化 metadata 的旧运行；PostgreSQL 适配器只用参数化 SQL 投影 snapshot/entry，并对同主键不同 hash 硬失败。
- 新增四张只读投影表迁移；正式记忆不提供 Dashboard 更新或删除接口，也不创建 Evidence/Conclusion 目录。

## 验证

使用 `.venv/bin/python` 运行 brief 四模块测试：12 项通过；未连接真实数据库、未联网、未读取真实 A 股数据。另执行 `git diff --check`。

## 风险与后续边界

Task7 的兼容路径对缺少 Task6 演化上下文的旧运行使用有限的 `unknown` 标签；这只支持历史运行可投影，不补造演化结论。正式服务器部署仍需 DBA 按迁移授予最小 SELECT 权限，并由服务器侧重放数据库投影。
