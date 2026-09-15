# Factor Miner 文档索引

本文按完整研究流程组织文档。第一次阅读建议先看[项目简介](../README.md)和[项目面试讲解](项目面试讲解.md)，再读[完整流程与日常使用](当前流程与日常使用.md)，了解论文、假设、公式、验证、回测、冗余与结果交付，再按问题查阅具体合同。

根目录另有[项目入口](../README.md)、[系统架构](../ARCHITECTURE.md)与[代理规则](../AGENTS.md)。真实候选、完整结果和本机位置见[A 股资料](../artifacts/A股/README.md)及[美股资料](../artifacts/美股/README.md)，公开克隆不包含这些真实产物。

## 阅读规则

- 使用说明解释系统流程，合同定义具体入口和版本，历史手册用于复现原运行。
- 探索资格、正式单因子通过、联合诊断、代表库采纳分别记录，不能因为文档放在同一目录就共用门槛。
- 下列策略、数据和研究入口都是能力目录；不是要求按顺序全部运行。
- 已有冻结参数、预算与结果继续保留；查看文档不自动启动研究。

## 系统说明与结果交付

- [项目面试讲解](项目面试讲解.md)：项目背景、60 秒介绍、常见追问和演示顺序。
- [Dashboard 的 API 研究与审核状态](runbooks/Dashboard的API研究与审核状态.md)
- [既有候选联合贡献事后诊断](contracts/既有候选联合贡献事后诊断.md)
- [联合诊断代表库采纳](contracts/联合诊断代表库采纳.md)
- [本地与服务器研究运行](runbooks/本地与服务器研究运行.md)
- [完整流程与日常使用](当前流程与日常使用.md)
- [因子宽表导出与模型接入](runbooks/因子宽表导出与模型接入.md)
- [项目目录与文件管理](项目目录与文件管理.md)

## 现有因子、组合与执行

- [八因子归因与策略诊断](contracts/八因子归因与策略诊断.md)
- [周频目标差额交易与十五因子目标](contracts/周频目标差额交易与十五因子目标.md)
- [多期限固定信号诊断](contracts/多期限固定信号诊断.md)
- [多期限组合预测对照](contracts/多期限组合预测对照.md)
- [截至2025年的八因子续研](contracts/截至2025年的八因子续研.md)
- [持仓价值未确定与轻量构念验证](contracts/持仓价值未确定与轻量构念验证.md)
- [既有信号持有周期与成本稳健性](contracts/既有信号持有周期与成本稳健性.md)
- [既有因子复核读模型](contracts/既有因子复核读模型.md)
- [既有因子联合岭回归](contracts/既有因子联合岭回归.md)
- [月末增量模型对照](contracts/月末增量模型对照.md)
- [月末目标差额执行](contracts/月末目标差额执行.md)
- [组合研究池与有限机制预算](contracts/组合研究池与有限机制预算.md)

## 数据发布与观测合同

- [公司 A 股数据适配合同](contracts/company-a-share-data.md)
- [历史行业同业收益发布](contracts/历史行业同业收益发布.md)
- [收盘报价发布与交易成本量级审计](contracts/收盘报价发布与交易成本量级审计.md)
- [日历月观察协议](contracts/日历月观察协议.md)
- [普通现金分配历史观察原型](contracts/普通现金分配历史观察原型.md)
- [月度成交观察与历史分组状态](contracts/月度成交观察与历史分组状态.md)
- [标准面板数据合同](contracts/标准面板数据合同.md)
- [每日市值与换手观察发布](contracts/每日市值与换手观察发布.md)
- [米筐Barra风险数据](runbooks/米筐Barra风险数据.md)

## 新增探索与研究治理

- [因子研究协议](constraints/FACTOR_RESEARCH_PROTOCOL.md)
- [研究治理](constraints/RESEARCH_GOVERNANCE.md)
- [A股双边万14成本与研究候选入库](contracts/A股双边万14成本与研究候选入库.md)
- [A股同假设多公式探索](contracts/A股同假设多公式探索.md)
- [FaVOR多期限执行协议](contracts/FaVOR多期限执行协议.md)
- [FaVOR测量合同预检与纠错复核](contracts/FaVOR测量合同预检与纠错复核.md)
- [受控研究运行合同](contracts/受控研究运行合同.md)
- [有限研究与资源复用](contracts/有限研究与资源复用.md)
- [跨市场假设约束与FaVOR联合研究](contracts/跨市场假设约束与FaVOR联合研究.md)
- [跨市场探索入口与确认分层](contracts/跨市场探索入口与确认分层.md)

## 测量算子与专项研究

- [共同下跌半贝塔](contracts/共同下跌半贝塔.md)
- [十二因子状态假设比较](contracts/十二因子状态假设比较.md)
- [单因子状态假设小实验](contracts/单因子状态假设小实验.md)
- [回撤后恢复测量](contracts/回撤后恢复测量.md)
- [市场分散程度上下文](contracts/市场分散程度上下文.md)
- [市场收益上下文](contracts/市场收益上下文.md)
- [收益显著性测量](contracts/收益显著性测量.md)
- [波动指数上下文](contracts/波动指数上下文.md)
- [滚动左尾同步](contracts/滚动左尾同步.md)
- [滚动左尾均值](contracts/滚动左尾均值.md)
- [滚动新增解释份额](contracts/滚动新增解释份额.md)
- [滚动末端残差](contracts/滚动末端残差.md)
- [滚动样本协方差](contracts/滚动样本协方差.md)
- [滚动残差波动](contracts/滚动残差波动.md)
- [石油ETF波动上下文](contracts/石油ETF波动上下文.md)
- [高低收盘价差代理](contracts/高低收盘价差代理.md)

## 历史决策与复现手册

- [2026 年 8 月批次技术报告（历史）](Factor%20Miner技术报告.md)
- [独立项目与 JSONL 账本决策](decisions/0001-standalone-project-and-jsonl-ledger.md)
- [从零因子发现重启协议](decisions/从零因子发现重启协议.md)
- [周频策略目标与诊断边界](decisions/周频策略目标与诊断边界.md)
- [因子状态分类与验证展示设计草案](decisions/因子状态分类与验证展示设计草案.md)
- [固定交易日标签与因果回测迁移](decisions/固定交易日标签与因果回测迁移.md)
- [Dashboard自主研究运行](runbooks/Dashboard自主研究运行.md)
- [候选筛选与稳健性报告（旧版复现）](runbooks/候选筛选与稳健性报告.md)
- [状态分类登记与展示](runbooks/状态分类登记与展示.md)
- [美股全库统一复核](runbooks/美股全库统一复核.md)
- [记忆辅助假设生成与120槽运行](runbooks/记忆辅助假设生成与120槽运行.md)
- [配对局部变异影子层（历史设计）](配对局部变异影子进化层技术设计.md)

## 文件组织

`contracts/` 保存数据与研究合同，`runbooks/` 保存具体操作说明，`constraints/` 保存通用约束，`decisions/` 保存历史决策。`evidence/` 保存已有技术报告的脱敏聚合材料，不作为当前真实候选库。其他预留目录未被提升为当前流程入口。

本次保留既有文件位置，通过索引和历史标签消除入口混淆；没有移动或删除真实数据、原研究产物或共享代码快照。
