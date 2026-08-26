# Task6 修复轮次二报告

## 研究边界

本轮只修复 strict generation seal 的内容寻址复验，不读取真实 A 股数据，不执行 IC、回测或生产有效性判断。seal 通过仍只表示生成身份和槽对象满足可见合同，不代表因子机制或 Alpha 结论。

## 修复内容

- `ResearchEvolutionContext` 与 `ApprovedEvolutionHypothesisBatch` 即使已经是构造好的模型实例，也必须先 `model_dump(mode="json")`，再调用 `model_validate()`。
- 因而 `model_copy(update=...)` 篡改 `context_sha256` 覆盖内容的对象会重新触发 identity validator 并硬失败。
- strict `slot_objects` 经过 JSON 重序列化、槽位身份、状态和 hash 格式校验，并与服务器不可变槽对象逐项比较，拒绝内容或身份不一致。
- legacy seal 输入路径保持兼容。

## 验证

focused strict 测试覆盖篡改 `design_policy_hash` 的首次 build，以及合法 context 的七项非空 seal；brief 七模块测试 29/29 通过，`git diff --check` 通过。未联网，未修改 Task5、CLI、Task7 或 `.task5-fix-round-3-tmp/`。
