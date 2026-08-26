# 任务五第一轮修复报告

## 修复范围

- H1：拆分演化请求的公开 prompt 与本地审计 payload。模型只看到脱敏 gap brief、允许字段别名、算子族、数据可用性标签、窗口带、结构轴，以及固定的 H01-H10 和三设计差异合同；context/family/gap/memory/brief hash、授权 scope、evolution binding、model scope 和 redaction policy 不进入 user message。审计 payload 仍保存这些绑定，并记录独立的审计 payload hash。
- H2：`approval_role` 经过去首尾空白后必须非空；批准入口要求授权或明确预期角色，并核验所有十个决定与授权角色一致。缺授权、空角色和越权角色均硬失败。
- H3：`build_expression_agent_request` 增加明确的演化路径。携带演化 context、family/hash 或批准批次时，必须先提供完整十槽且全部 `approved` 的 `ApprovedEvolutionHypothesisBatch`；context、family 或批准 batch hash 不一致硬失败。未携带演化绑定的旧 Stage C 调用继续使用 legacy 路径。

## 验证

完整 brief 五模块（第一轮当前工作区）：

```text
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_research_evolution tests.test_llm_agents tests.test_llm_online tests.test_llm_hypothesis tests.test_llm_literature
............................
----------------------------------------------------------------------
Ran 28 tests in 0.007s

OK
```

第一轮记录的 28 项是包含用户 A/B/C 当前脏改动的工作区事实；第二轮以 `e3ca5fa` 为基线并只应用 Task5 staged patch 的 clean snapshot 为 26 项通过。差异来自用户 A/B 的在线测试以及本轮从 index 移除的误带 `format_repair` 测试。

H1-H3 focused 覆盖已包含在上述模块中，新增断言包括 prompt 不含内部元数据及其值、空白/越权审批角色、无批准 expression 阻断、完整批准 batch 放行和批准 hash 不一致阻断。

`git diff --check` 通过；未调用网络或真实 LLM。

## 脏改动边界

用户 A/B 的 `llm_agents.py`、`llm_online.py` 及相关测试脏改动均保留。本轮只追加 Task5 自己的修复 hunks；未修改 CLI、orchestrator 或其他用户文件。
