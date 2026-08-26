# Task7 修复 Round1 完成报告

## 研究边界

本轮只修复发布顺序、研究记忆引用完整性和 Dashboard 四表只读投影，不执行真实 A 股计算、统计评价、机制检验或生产结论判断。正式记忆仍只保存槽位身份、状态、离散标签、有限摘要和哈希；原始行情、逐日输出和外部模型原文不进入 memory 或数据库。

## 修复内容

- `publish_campaign_memory` 先原子追加 `entries.jsonl`，成功后才写 snapshot/reference；追加失败不会留下悬空引用。重复重放会在缺失 snapshot/reference 时补齐，不重复追加条目。
- `publish_campaign_evaluation` 不再吞掉数据库异常；正式 JSON/JSONL 已保留，reference 保持 `pending`，投影异常向调用方暴露。
- gap 计数使用固定 `GapCategory` 三类初始化，不依赖 `gap_cards[0]`。
- reference 现在完整携带 context、approval、coverage graph、source memory snapshot、gap report、design policy、approval 数量、gap 脱敏摘要、120 槽终态计数和 snapshot identity，并由 `PublishedCampaignMemoryReference` 校验。
- migration 006 与 PG 投影补齐 snapshot graph/source-family/cutoff identity、entry snapshot/graph/结构标签字段；先插 `research_memory_snapshots` 父表，再插受非延迟 FK 约束的 entries 子表。四表保留主键/hash 冲突硬失败和同 hash 幂等。

## 验证

brief 四模块测试 15 项通过；相关记忆、runner、schema、Dashboard 回归 34 项通过；`git diff --check` 和 Python 编译检查通过。测试使用合成 fake store/SQL 记录器验证四表投影与父子顺序，未连接真实数据库、未联网、未读取真实 A 股。

## 风险边界

严格演化发布必须同时传入与 seal 一致的 context、approval batch 和 gap report；旧运行继续使用无演化 metadata 的兼容路径。数据库投影不是正式主存储，失败时需由服务器侧重放投影。
