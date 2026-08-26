# 任务三报告：正式研究记忆追加写入与不可变 snapshot

## 修改文件

- `src/factor_miner/research_memory.py`
- `src/factor_miner/ledger.py`
- `src/factor_miner/research_evolution_schema.py`
- `src/factor_miner/runtime.py`
- `tests/test_research_memory.py`
- `tests/test_research_evolution_schema.py`

## 布局与不变量

- 正式布局固定为：
  - `<artifact_root>/research_memory/entries.jsonl`
  - `<artifact_root>/research_memory/entry_objects/<memory_entry_id>.json`
  - `<artifact_root>/research_memory/snapshots/<memory_snapshot_id>.json`
  - `<artifact_root>/research_memory/index.json`
- `entries.jsonl` 使用复用后的单写入者哈希链；每条事件保存 `event_id`、`memory_entry_id`、`entry_sha256`、对象相对路径、写入时间与 `supersedes_entry_id`。
- 追加与索引更新现在处于同一 entries lock 内：`HashChainJsonlStore.with_locked_events(...)` 在单次非阻塞 flock 中完成“读取最新事件 → 受控追加 → 基于追加后完整事件写 index”，避免两个 writer 交错时用陈旧 `events` 覆盖 `index.json`。
- entry object 与 snapshot file 一旦写入，只允许逐字节复验复用，禁止覆盖。
- `index.json` 由正式 entries / snapshots 重建后原子改写；`load_entry` / `load_snapshot` 不依赖 index，可直接从正式文件恢复。
- `append_entry` 在写入前强校验：
  - 污染 family `llmfamily_1bae19965638a6ac9620e0b0` 拒绝；
  - `ResearchMemoryEntry` 内容 hash 必须成立；
  - family → generation seal → candidate slot → published run → `run/input_manifest.json` 的身份链必须一致；
  - `PublishedRunInputManifest` 兼容当前正式发布清单已存在的 `generation_manifest_sha256`、`evaluation_policy_id`、`statistical_budget_hash`，同时继续拒绝未登记额外字段；
  - `run/input_manifest.json` 现在正式要求 `published_at`（ISO 且带时区）；
  - 缺 published input manifest、缺 `data_contract_identity_hash`、缺 `published_at`、同 ID 异内容、对象 hash 不一致都会硬失败。
- `build_snapshot` 在 cutoff 前选择已完成且已发布的 entry，显式要求：
  - `source_family_ids` 非空、唯一、排序稳定；
  - family 必须属于当前策略白名单；
  - 污染 family 显式拒绝；
  - coverage graph 必须能按 `<artifact_root>/artifacts/coverage_graphs/<coverage_graph_id>` 复验；
  - 只有 `entry.created_at <= cutoff` 且 `run/input_manifest.json.published_at <= cutoff` 的条目才允许进入 snapshot；
  - snapshot hash 必须可重算一致。
- `ResearchMemorySnapshot` 新增冻结 `entry_bindings`，显式保存 `(memory_entry_id, entry_sha256)` 配对：
  - 非空 snapshot 必须携带完整 bindings；
  - `entry_ids`、`entry_hashes` 必须与 `entry_bindings` 一一对应；
  - bindings 按 `memory_entry_id` 严格排序且不可重复；
  - 篡改 `entry_ids`、`entry_hashes` 或 bindings 任一侧都会硬失败。
- `verify()` 现在要求 snapshot binding 必须回连当前正式账本：
  - `memory_entry_id` 必须存在于当前 `entries.jsonl`；
  - binding hash 必须与当前正式 event hash / object hash 完全一致；
  - 仅修改 snapshot 与 index 形成“内部自洽”也会被硬失败。
- `runtime.py` 新增 `resolve_research_memory_root(...)` 与 `RuntimeProfile.research_memory_root`，把 research memory 根目录显式固定在 `<artifact_root>/research_memory`，不再依赖仓库根或当前工作目录隐式推断。

## 测试命令

```text
env UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache PYTHONPATH=src uv run python -m unittest tests.test_research_memory tests.test_ledger tests.test_llm_ledger tests.test_research_evolution_schema
git diff --check
```

## 实际输出

```text
.....................................
----------------------------------------------------------------------
Ran 37 tests in 0.050s

OK
```

## concerns

- 后续任务七发布流程必须把正式 `published_at` 写入 `run/input_manifest.json`；本任务已经把它当作 research memory / snapshot cutoff 的正式契约，但按要求未提前修改任务七发布代码。
- `ResearchMemorySnapshot` 目前仍保留任务一的 `entry_ids`/`entry_hashes` 字段以兼容既有调用方，因此实现是“先按 `(created_at, memory_entry_id)` 冻结可选集合，再以 `entry_bindings` 保存正式配对，并按 `memory_entry_id` 落盘 identity 相关字段”；如果后续要把“时间顺序本身”也作为 snapshot 外部合同，需要在任务一冻结 schema 上继续演进。
