# 任务四修复轮二报告：字段注册表硬闸门

## 修复内容

- `preflight_designs(...)` 保留 `registry=None` 的调用签名兼容性，但 `None` 现在立即以 `FIELD_MISSING` 硬失败。
- 删除按 AST 字段伪造 `synthetic` 字段注册表的路径，避免 unknown field、单位不相容、PIT 和字段准入校验被伪合同绕过。
- 真实 `FieldAvailabilityRegistry` 仍转换为 local field ID 视图，保留原始字段的单位、PIT、可用性、数据发布和注册表身份信息。
- `preflight_candidate_batch(...)` 继续把已有真实 `registry` 显式传入公开设计闸门。
- direct-call 测试统一传入脱敏 synthetic registry，并新增缺 registry、unknown field 和单位不相容的失败断言。
- 保持 temporal-only 结构轴失败记录使用 `ast_without_temporal_parameters_hash`，没有回退到完整 canonical hash。

## 接口影响

公开 `preflight_designs(...)` 的 `registry` 参数仍可省略以保持 Python 调用兼容，但省略不会再得到 `DesignDiversityResult`，而是抛出 `FactorMinerError`，其 `code` 为 `FIELD_MISSING`。所有希望执行设计差异检查的正式调用方必须提供真实的 `FieldAvailabilityRegistry`；本轮没有修改 Task 6 的 CLI 或 orchestrator wiring。

## 测试命令与实际输出

```text
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_design_diversity tests.test_dsl tests.test_dsl_semantics tests.test_llm_candidate
............................
----------------------------------------------------------------------
Ran 28 tests in 0.003s

OK
```

```text
git diff --check -- src/factor_miner/design_diversity.py src/factor_miner/dsl.py src/factor_miner/llm_candidate.py tests/test_design_diversity.py
```

命令无输出并成功返回。

## 剩余风险

- 本轮没有改动 Task 6 负责的 120 槽外层编排；外层若未提供真实 registry，现会显式失败，不会静默放行。
- 当前设计闸门的验证错误仍沿既有 `FactorMinerError` 直接抛出，失败槽编排和失败事件接收由后续任务负责。
