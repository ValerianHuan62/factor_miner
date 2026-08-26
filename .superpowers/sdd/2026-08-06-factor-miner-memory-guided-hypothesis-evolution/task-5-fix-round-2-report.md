# 任务五第二轮修复报告

## 修复范围

- 为 `PreparedDeepSeekRequest` 增加可选的 `audit_payload_sha256`，并校验它与 `export_payload` 的规范 JSON 哈希一致。
- `build_deepseek_request` 增加可选 `audit_payload`：公开的 `user_payload` 继续进入 DeepSeek body；`audit_payload` 进入本地 `export_payload` 并计算审计哈希。未提供时审计 payload 默认使用 `user_payload`，保持既有调用兼容。
- 从 Task5 commit 范围仅移除 `tests/test_llm_agents.py` 中误带的 `format_repair` 测试；用户 A/B 的 import、函数和测试继续保留在工作区，不进入本次提交。
- 未修改 CLI、orchestrator 或用户 A/B/C 的其他文件。

## 验证

精确测试命令：

```text
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_research_evolution tests.test_llm_agents tests.test_llm_online tests.test_llm_hypothesis tests.test_llm_literature
```

- 当前含用户 A/B/C 脏改动的工作区：28 项通过。
- `e3ca5fa` clean snapshot 加本轮 staged patch：26 项通过。
- `git diff --check`：通过。
- 未调用网络或真实 LLM；测试仅使用合成数据。

## 索引与工作区边界

- 本次提交只包含 `src/factor_miner/llm_online.py` 的 19 行最小审计合同 hunk、`tests/test_llm_agents.py` 的 gate 测试清理，以及三份 Task5 流程报告。
- `llm_online.py` 其余用户 A/B scope authorization 等改动保持 unstaged。
- 用户 A/B/C 的其他未提交文件保持原状；工作区中的 A/B `format_repair` 内容未被覆盖。

## 结论边界

本轮只修复请求内容身份和 clean snapshot 测试边界；测试通过不代表在线模型输出具备金融有效性、机制认证或生产资格。
