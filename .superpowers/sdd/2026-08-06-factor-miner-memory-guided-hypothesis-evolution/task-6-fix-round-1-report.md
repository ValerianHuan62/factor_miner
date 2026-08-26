# Task6 修复轮次一报告

## 研究边界

本轮只修复 generation seal 的演化上下文传递，不读取真实 A 股数据，不执行 IC、回测或生产有效性判断。seal 通过只表示生成身份与 120 槽终态满足可见合同，不构成因子机制或 Alpha 结论。

## 修复内容

- `seal_completed_generation()` 在入口统一调用 `_strict_seal_inputs()`。
- strict 路径必须同时提供 `approval_batch`、`evolution_context` 和 `slot_objects`；缺任一项硬失败。
- build 与已封存 replay 的 `verify_generation_seal()` 复用同一组批准批次、演化上下文和槽对象。
- generation seal 继续绑定并复验 family、coverage graph、memory snapshot、gap report、design policy、approval batch 与逻辑假设 ID 的七项 hash。
- legacy seal 输入保持兼容，不会被 strict 字段半填充静默降级。

## 验证

使用纯合成槽对象运行 focused strict build/replay 测试及 brief 七模块测试；graph、memory、gap 任一 hash 被篡改时 replay 均失败。未联网，未修改 CLI、Task5、Task7 或 `.task5-fix-round-3-tmp/`。
