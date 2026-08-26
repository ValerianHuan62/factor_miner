# 任务四报告：三因子设计实质差异闸门

## 修改文件

- `src/factor_miner/dsl.py`
- `src/factor_miner/design_diversity.py`
- `src/factor_miner/llm_candidate.py`
- `tests/test_design_diversity.py`

## 签名算法

- 先对每个 local typed AST 执行既有硬校验路径：`validate_candidate_expression` → `analyse_semantic_type` → `validate_ast` → `compile_candidate`。
- 完整签名 `canonical_ast_hash` 继续使用既有 `canonical_ast` + `sha256_json`。
- 结构签名 `ast_without_temporal_parameters_hash` 基于新的 `canonical_ast_without_temporal_parameters`：
  - 先生成 canonical AST；
  - 递归删除 `period`、`window`；
  - 保留 `op`、`args`、`field`、`value` 等非时间结构信息；
  - 再做 `sha256_json`。
- 其余结构轴：
  - `field_set`：子树字段集合，排序后稳定输出；
  - `operator_topology`：canonical AST 前序遍历的算子序列；
  - `temporal_roles`：canonical AST 中时间/rolling 节点的稳定路径加算子名；
  - `input_combinations`：每个内部节点聚合到的字段集合；
  - `node_count`：canonical AST 节点数；
  - `depth`：canonical AST 深度。

## 失败语义

- `canonical_ast_hash` 重复：判定为 exact AST duplicate，返回 `DESIGN_DIVERSITY_FAILED`。
- `ast_without_temporal_parameters_hash` 重复：判定为 temporal-only change，说明只改了 `window`/`period` 等时间参数，返回 `DESIGN_DIVERSITY_FAILED`。
- 失败结果为可序列化对象，逐条记录：
  - `left_index` / `right_index`
  - `left_slot_id` / `right_slot_id`
  - `axis_name`
  - `left_hash` / `right_hash`
  - `failure_code`
- centered rolling、未知字段、超 lookback 等错误不会被吞掉；在生成签名前就沿既有异常路径直接失败。

## 测试命令

`PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_design_diversity tests.test_dsl tests.test_dsl_semantics tests.test_llm_candidate`

## 实际输出

```text
........................
----------------------------------------------------------------------
Ran 24 tests in 0.002s

OK
```

## Concerns

- 按 task-4 brief 的文件边界，本次没有修改 `src/factor_miner/cli.py` 或 `src/factor_miner/llm_orchestrator.py` 现有调用点；因此“进入候选槽终态前必须经过闸门”的接线被实现为 `src/factor_miner/llm_candidate.py` 内新增的批量 `preflight_candidate_batch` 入口，外层流程接入需由后续允许修改调用点的任务完成。
