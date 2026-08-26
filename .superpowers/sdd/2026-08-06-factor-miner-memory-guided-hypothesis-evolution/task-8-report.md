# Task 8 中文报告

## 研究边界

本任务只补记忆辅助假设生成的正式 CLI、人工十槽批准闸门、运行手册、运行合同和批次总览展示。输出仍只能称为通过冻结可见验证的候选因子，不构成有效 Alpha、因果机制或生产结论；真实数据与统计运行继续受公司 Linux 和既有 Stage A/B/C 合同约束。

## 实现摘要

- 新增 `evolution prepare-context`、`generate-hypotheses`、`approve-hypotheses`。
- 上下文绑定指定 graph、cutoff 前 memory、gap 和冻结政策身份；草案产物保存请求 hash、响应摘要、模型身份和脱敏 prompt，不保存模型原文。
- 批准入口逐条校验十个槽、草案 hash、context hash、family 和人工角色；Stage C 新参数先过严格演化校验，再调用既有冻结运行器。
- 手册补充 gap → 十假设 → 人工批准 → 三设计签名 → expression/backtest → seal/memory/Dashboard 和纠正事件流程。
- Dashboard 批次总览在存在演化元数据时展示 context、批准数量和 120 槽终态数量。

## 验证边界

本地只使用脱敏/合成输入；未执行公司服务器真实候选计算、IC、回测或发布。
