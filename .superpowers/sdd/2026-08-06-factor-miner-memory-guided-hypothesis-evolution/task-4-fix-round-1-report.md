# 任务四修复一轮报告

## 修复范围

- `src/factor_miner/design_diversity.py`
- `src/factor_miner/llm_candidate.py`
- `tests/test_design_diversity.py`

## 本轮修复内容

### 1. `preflight_designs(...)` 自身补齐硬校验链路

- 公开入口 `preflight_designs(...)` 不再直接对输入表达式做 `_build_signature(...)`。
- 新增 local 校验路径：
  - `analyse_semantic_type(...)`
  - `validate_ast(...)`
  - `compile_candidate(...)`
- 对 direct-call local AST：
  - 若调用方未提供 `registry`，函数会按 AST 中出现的 local 字段名构造最小 synthetic local registry；
  - 若调用方提供 `registry`，函数会转换成 `field_id -> field_id` 的 local 视图，再做同等硬校验。
- 因此 `center=True`、未来信息、字段不可用、lookback 超限、compiler 失败都不会再被 direct-call 绕过。

### 2. 失败轴 hash 改为记录实际触发轴

- `canonical_ast_hash` 轴重复时，`left_hash/right_hash` 记录完整 canonical hash。
- `ast_without_temporal_parameters_hash` 轴重复时，`left_hash/right_hash` 记录去时间参数后的结构 hash。
- 这样审计记录可以直接显示真正触发重复判定的轴值。

### 3. 直接调用 `preflight_designs(...)` 的非法表达式测试

- 新增 direct-call centered rolling 测试。
- 断言 `preflight_designs(...)` 直接抛出 `LOOKAHEAD_DETECTED`，不会返回 `passed=True`。
- 同时补充 temporal-only failure 的轴 hash 断言。

## 测试命令

`PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_design_diversity tests.test_dsl tests.test_dsl_semantics tests.test_llm_candidate`

## 实际输出

```text
.........................
----------------------------------------------------------------------
Ran 25 tests in 0.003s

OK
```

## diff 检查

`git diff --check -- src/factor_miner/dsl.py src/factor_miner/design_diversity.py src/factor_miner/llm_candidate.py tests/test_design_diversity.py`

输出为空，通过。

## 仍存风险

- `preflight_designs(...)` 在未提供真实字段注册表时，会使用 synthetic local registry 完成 direct-call 硬校验；这能保证 centered rolling / lookback / compiler / DSL 形状等硬失败不被绕过，但字段经济语义与单位相容性只能在调用方提供真实 `registry` 时完全受真实合同约束。
- 按 Task 4 文件边界，本轮仍未修改 CLI / orchestrator 外层 wiring；当前公开批量接线点仍是 `preflight_candidate_batch(...)`。
