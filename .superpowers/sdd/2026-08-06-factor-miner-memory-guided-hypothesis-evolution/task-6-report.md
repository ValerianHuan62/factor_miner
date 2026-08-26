# Task6 完成报告

## 研究边界

本任务只绑定演化上下文、批准假设、生成槽位和 generation seal，不读取真实 A 股数据，不执行 IC、回测或生产有效性判断。任何通过都只能解释为通过可见生成阶段校验的候选因子；三设计闸门失败表示设计不满足冻结的结构多样性合同，不代表经济机制或 Alpha 结论。

## 实现结果

- strict evolution 路径核验完整 H01-H10 批次，并要求两条 LLM arm 共享同一 `ResearchEvolutionContext` 与批准批次身份。
- 固定四条 arm、每个逻辑假设三个设计槽，生成前登记并在最终收口时要求 120 个槽全部终态。
- strict 路径对每组三设计复用 `preflight_designs`；仅改变 window/period 的设计写入 `DESIGN_DIVERSITY_FAILED`，保留槽位、逻辑假设、上下文和失败签名，且不写评价依赖区间。
- generation seal 绑定并复验七项演化身份哈希：上下文、coverage graph、memory snapshot、gap report、approval batch、design policy 和逻辑假设 ID。
- legacy 非演化 Stage C 调用保持原有行为。

## 验证

使用本地合成夹具运行 brief 指定测试；未联网、未访问真实 A 股数据。统计分母仍固定为 120，失败槽位保留在账本中，不从多重检验预算中移除。
