# Task 9 第四轮修复报告

## 研究边界

本轮只修复正式研究记忆跨轮快照的计数引用一致性和合成投影验收，不读取真实 A 股行情，不计算真实 IC、冗余或回测。测试使用程序生成的 family、generation seal、运行清单和覆盖图谱；通过结果只能说明工程链路具备可见验证条件，不能说明因子有效、Alpha 或机制成立。

## 修复内容

1. `publish_campaign_memory` 在 `publish_batch` 已锁定的 current events 上恢复正式 typed entries，并严格按新 snapshot 的 `entry_ids` 计算 `entry_count` 和 `terminal_counts`。
2. reference 的 `memory_snapshot_sha256` 继续直接绑定正式 snapshot hash，reference hash 在全量计数写入后重新计算；不改变每轮追加 120 条 typed entry、历史保留、旧 snapshot 不重写和相同重放幂等语义。
3. 新增跨轮合成验收：连续正式发布两轮各 120 条，断言第二轮 snapshot/reference 为 240 条、终态计数覆盖两轮全量，并验证 `ResearchMemoryStore.verify/load_snapshot` 与 Dashboard loader 的计数一致。
4. 修复 PostgreSQL migration 006：新建和旧安装均使用 `entry_count >= 0`；升级路径仅删除精确匹配的旧 `entry_count = 120` check，并新增稳定命名约束。Dashboard 投影正式 snapshot 的全部 typed entries，不再按当前 `run_id` 丢弃历史条目；fake PG 测试验证第二轮 240 条 child rows、四表身份和父表先行。

## 验证

- 相关测试：`./.venv/bin/python -m unittest tests.test_research_memory_projection -v`，12 个测试通过。
- 全量 `./.venv/bin/python -m unittest discover -v`：503 个测试通过。
- `git diff --check`：通过。

结论仍限于“通过可见验证的候选因子”所需的工程闭环，本报告不提供金融研究结论。
