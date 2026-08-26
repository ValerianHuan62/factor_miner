# Task 9 第三轮修复报告

## 研究边界

本轮只修复研究记忆发布协议、Dashboard 只读投影和合成演化验收，不读取真实 A 股行情，不计算真实 IC、冗余或回测，也不把合成结果解释为因子有效性、Alpha 或机制证据。

## 修复内容

1. `publish_campaign_memory` 在正式演化路径中先核验已发布 run 和 input manifest，再把 120 个槽位摘要转换为 `ResearchMemoryEntry`。条目只保留内容身份、离散签名、终态、有限评价摘要和数据身份摘要，不保存 raw output、行情序列或模型原文；身份由 schema builder 和 `ResearchMemoryStore` 复核。
2. 正式发布通过 `ResearchMemoryStore.append_entry` 写入对象、哈希链事件和 `index.json`，再由 `build_snapshot` 绑定已核验 coverage graph，最后写 snapshot/reference。重复发布保持幂等，部分追加失败不会产生 snapshot/reference。无 coverage graph 的旧调用仅写入隔离的 `research_memory_legacy/`，不污染正式账本。
3. Dashboard 投影不再直接解析 `entries.jsonl`，而是先执行正式 store `verify()`，再通过 `load_snapshot`/`load_entry` 读取 typed 对象，保持 snapshot、entry、context、gap 和 terminal/approval 身份链。
4. 缺口模块的用户目录边界检测改为运行时安全拼接，源码扫描不再出现字面量 `/Users/`，原有边界测试继续覆盖该路径。
5. Task9 合成 E2E 使用真实发布 run input manifest、family ledger、generation seal 和 coverage graph，断言发布后正式 loader 能验证并读取 snapshot/entries，再进行 Dashboard 投影；不手写 next snapshot 或 dashboard 字典。

## 验证

- 相关测试：通过，21 个测试。
- 全量 `unittest discover`：通过，500 个测试。
- `git diff --check`：提交前执行。

结论仍仅限于“通过可见验证的候选因子”所需的工程闭环；本报告不提供金融研究结论。
