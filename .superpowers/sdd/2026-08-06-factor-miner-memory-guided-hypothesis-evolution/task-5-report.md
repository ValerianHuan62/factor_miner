# 任务五报告：十逻辑假设、人工批准与双 arm 复用

## 修改文件

- `src/factor_miner/research_evolution.py`
- `src/factor_miner/llm_online.py`
- `src/factor_miner/llm_hypothesis.py`
- `src/factor_miner/llm_literature.py`
- `tests/test_research_evolution.py`
- `tests/test_llm_online.py`
- `tests/test_llm_hypothesis.py`
- `tests/test_llm_literature.py`

## 本次收束范围

- 新增 `research_evolution.py`，实现：
  - 十槽 `H01`–`H10` 的固定脱敏请求；
  - 严格响应解析；
  - 人工批准批次核验；
  - `approval_required` / `hypothesis_budget_invalid` 稳定失败语义。
- 在 `llm_online.py` 增加演化请求专用授权：
  - 绑定 `context_sha256`、`gap_report_hash`、`memory_snapshot_hash`、`brief_sha256`；
  - 固定 `hypothesis` 角色、模型和带时区有效期；
  - 校验 `evolution_binding` 与 `model_scope`。
- 在 `llm_hypothesis.py` 增加最小两 arm 适配层：
  - 同一个逻辑批准批次可显式适配为 `coverage_outcome_llm` 与 `literature_only_llm`；
  - 两个 arm 共享 `logical_hypothesis_id`、`approval_batch_sha256`、`context_sha256`；
  - 仅 `slot_id`、`generation_route` 和文献任务来源不同。
- 在 `llm_literature.py` 增加 source-record 任务投影：
  - 文献检索只接收逻辑假设冻结出的 `LiteratureSourceRecordTask`；
  - 不再重生成第二套假设。

## 关键合同

- 请求只外发 gap card 白名单字段；额外历史数值、路径、原始数据键会被过滤，不进入 prompt。
- 响应必须：
  - 正好 10 条；
  - 完整覆盖 `H01`–`H10`；
  - 拒绝重复槽位；
  - 拒绝重复内容；
  - 拒绝重复 gap 充数；
  - 拒绝 `alpha`、`HAC`、`multiplicity_family_id`、数据合同等覆盖键。
- 批准必须：
  - 十槽齐全；
  - 每槽恰好一个 `approved`；
  - `draft_sha256` / `context_sha256` / `discovery_family_id` 全部匹配；
  - 审批角色必须非空、去首尾空白后稳定，并匹配授权角色；
  - `approved_at` 必须带时区；
  - 不满足时返回稳定 `APPROVAL_COUNT_INVALID`，消息为 `approval_required` 或 `hypothesis_budget_invalid`。
- 表达式入口现在区分 legacy Stage C 与演化路径；演化路径必须携带完整十槽批准批次，批准前或绑定不一致时硬失败。

## 测试命令

`PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_research_evolution tests.test_llm_agents tests.test_llm_online tests.test_llm_hypothesis tests.test_llm_literature`

## 实际输出（第一轮工作区）

```text
............................
----------------------------------------------------------------------
Ran 28 tests in 0.007s

OK
```

## 第二轮验证事实

- 当前包含用户 A/B/C 脏改动的工作区执行上述精确测试为 28 项通过。
- 以 `e3ca5fa` 为基线、仅应用本轮 staged patch 的 clean snapshot 执行上述精确测试为 26 项通过。
- clean snapshot 不包含用户 A/B 的 `tests/test_llm_online.py` 新测试，也不包含误带入的 `format_repair` 测试；后者已仅从 index 移除，工作区原内容保留。

## 代码卫生

- `git diff --check` 通过。
- `llm_online.py` 与 `tests/test_llm_online.py` 在本任务开始前已存在用户 A/B 未提交修改；本次实现未覆盖它们，只在其后追加 Task 5 所需最小 hunks。

## 第一轮修复

- H1：user prompt 只保留脱敏 gap brief、公开 alias/算子/可用性/窗口/结构轴和十槽/三设计合同；family、各类 hash、scope、binding、model scope 和 redaction policy 只保留在本地审计 payload，并绑定审计 payload hash。
- H2：`approval_role` 去首尾空白且不得为空；批准入口必须提供授权或预期角色，并要求十条决定与该角色一致。
- H3：演化 expression builder 无批准批次、非十槽或 context/family/approval hash 不一致时稳定硬失败；旧非演化调用保持兼容。

## 未做的事

- 未修改 `cli.py`、`llm_orchestrator.py`、`research_campaign_runner.py` 等接线层。
- 未接入表达式生成、回测或发布流程；Task 5 只实现“批准前硬阻断”和旧 arm 适配准备层。
