# Task9 修复轮次一报告

## 研究边界

本轮只验证脱敏合成的人工批准闸门、研究记忆快照边界和 Dashboard 脱敏投影合同。测试没有读取真实行情，没有计算真实 IC、显著性、冗余或回测；合成通过不能推出 A 股因子有效性、经济机制或生产结论。研究记忆中的评价字段只是程序生成的离散摘要，机制仍不构成已验证机制。

## 修复内容

1. `test_nine_approvals_stop_before_expression_or_publication` 改为使用 `CliRunner` 调用真实 `evolution approve-hypotheses` 命令。测试构造合法 context、draft batch 和带逐条 hash 的 9 条 decisions，断言非零退出、approved output 不存在，并比较 CLI 前后 runtime、expression、backtest、publication 相关路径没有新增文件。
2. 120 槽闭环新增真实 `ResearchMemoryStore.append_entry()`、`build_snapshot()` 和 `verify()`。同轮条目在 `cutoff=NOW` 的 snapshot 中可见；下一轮使用严格早于该条目的时间 cutoff，断言 entry 被排除且 snapshot 不包含它。
3. Dashboard 测试使用真实 `publish_campaign_memory()`、`publish_run_artifacts()` 和 `project_run_artifacts()` 生成读模型，再把真实投影 payload 交给 fake connection 上的 `PostgresDashboardStore._project_evolution_memory()`。断言演化批次、gap 摘要、memory snapshot、memory entries 四类表均被投影，`approval_count=10`、`slot_count=120`、memory/context/gap 身份可见；扫描 payload 确认不含模型原文、secret、服务器路径、市场/个股样本或 raw model response。
4. 污染 family 通过真实 `publish_campaign_memory()` 入口拒绝，并断言 reference 文件未写入。

## 精确验收命令与结果

- `./.venv/bin/python -m unittest tests.test_research_evolution_synthetic_e2e -v`：2 个测试通过。
- `./.venv/bin/python -m unittest tests.test_research_evolution_synthetic_e2e tests.test_research_memory tests.test_research_memory_projection tests.test_research_evolution_cli -v`：56 个测试通过。
- `./.venv/bin/python -m unittest discover -v`：500 个测试中 499 个通过、1 个失败；失败为已知基线 `tests.test_import_boundary.ImportBoundaryTest.test_source_does_not_reference_forbidden_dependencies_or_paths`，命中源码中的 `/Users/`。本轮未修改产品代码，也未声称全量通过。
- `./.venv/bin/python -m unittest tests.test_import_boundary -v`：同一已知基线失败，未纳入本轮修复范围。
- `git diff --check`：通过。

## 约束确认

本轮只修改 `tests/test_research_evolution_synthetic_e2e.py` 与本中文报告；没有修改产品代码，没有触发真实行情、IC 或回测，也没有提交 `.task5-fix-round-3-tmp/`。结论仅限于上述合成闸门和脱敏投影链路。
