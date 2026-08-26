# Factor Miner V0.5 受控大语言模型假设生成技术设计

- 状态：第三版待用户复核
- 日期：2026-07-30
- 前置版本：V0.4 双层因子覆盖图谱
- 首个外部模型：DeepSeek V4 Pro

## 1. 目标与金融边界

V0.5 使用冻结的 V0.4 覆盖图谱寻找现有因子库覆盖稀疏的研究方向，再通过三个职责隔离的大语言模型代理形成可证伪金融假设和白名单 typed AST 候选。

V0.5 解决的是“下一批值得研究什么”，不是“什么因子已经有效”。覆盖稀疏不等于存在 Alpha，文献支持不等于机制成立，大语言模型的 semantic lint 也不等于统计验证。最终产物只能称为：

- 大语言模型生成的假设草案；
- 人工批准但机制尚未验证的冻结假设；
- 大语言模型生成的待验证候选因子。

任何候选仍须进入既有候选登记、编译、服务器可见评价、HAC、Bonferroni、输出冗余和 V0.2 残差增量信息协议。V0.5 不自动把候选写入生产因子库，不产生认证因子、有效 Alpha、证据或生产结论。

## 2. 已批准决策

V0.5 冻结以下决策：

1. 使用方案 B：三个隔离代理、固定程序编排和人工假设闸门；
2. 外部模型使用 DeepSeek 官方 API 的 `deepseek-v4-pro`；
3. 开启 thinking，固定 `reasoning_effort=high`；
4. 每批最多 10 个假设槽，每个获批假设最多 3 个候选槽；
5. 拒绝、空缺、重复、非法和失败输出继续占用原槽，禁止补抽；
6. 假设通过人工批准后，表达式代理才可运行；
7. 经济学假设代理可使用受限文献检索网关，另外两个代理不能联网；
8. 每个研究批次显式冻结一个覆盖图谱快照，不自动按月刷新；
9. 首批使用 `covgraph_3e3b6821f534dad40b7369d7`；
10. 任何真实外发前必须通过本地脱敏预览和哈希授权；
11. 同一可见数据上的全部自适应生成必须归入一个预先冻结的发现研究族，禁止通过新建 campaign 重置多重检验；
12. 覆盖图谱驱动的假设固定标记为 `coverage_outcome_informed`，只能作为 discovery；
13. 每个假设必须包含机器可执行的 `TestablePredictionSpec`；
14. 候选 availability 必须由字段注册表和 AST 确定性推导；
15. 公司聚合衍生研究信息只有在具备独立公司外发授权时才能发送；
16. V0.5 同时运行同预算 shadow benchmark，验证 LLM 相对简单生成基线是否有增量。
17. 覆盖图谱的发现上下文与候选统计评价使用两个独立数据身份；若两者复用，结果只能称为探索性筛选；
18. 120 个候选槽全部进入不可变生成终态并完成 family seal 后，评价器才允许读取任何候选 outcome。

## 3. 不在 V0.5 范围内

V0.5 不实现：

- 任意网页浏览、浏览器控制或开放式网络代理；
- 大语言模型读取公司服务器、文件、数据库、Git、终端或环境变量；
- 大语言模型读取个股数据、完整每日 IC、完整相关矩阵、标签或密封样本外结果；
- 自主修改 DSL、评价政策、HAC、Bonferroni、V0.2 增量协议或研究预算；
- 长期记忆、自主循环挖掘、动态补抽或根据中间 IC 调整下一轮；
- PostgreSQL、分布式队列、多进程写入器或常驻代理服务；
- 自动执行策略回测、成本容量或生产入库；
- 自动刷新覆盖图谱或将当前批次结果递归喂回同一批次。
- 把 LLM semantic lint 当成独立证据、外部验证或机制认证。

## 4. 总体架构

```text
冻结 V0.4 覆盖图谱
  → 本地固定程序生成覆盖空白卡片
  → 白名单导出和敏感信息扫描
  → 人工核对外发预览并按哈希授权
  → 代理 1：经济学假设与受限文献检索
  → 保存固定 10 个草案槽
  → 人工核验论断支持、批准、拒绝或追加更正版本
  → 代理 2：每个获批假设最多 3 个 typed AST 草案
  → 固定程序执行 Schema、语义类型、字段 availability、前视、lookback、重复与编译检查
  → 代理 3：无证据权重的 LLM semantic lint
  → 形成 CandidateFactorSpec 待登记包
  → 四个 arm 的 120 个槽全部进入生成终态
  → 固定程序生成不可逆 family seal
  → 评价器核验 seal 后才开放公司服务器候选评价
```

大语言模型只生成结构化建议。读取文件、生成摘要、调用公开文献接口、校验输出、修改状态和发布产物都由固定程序完成。

## 5. 隐私威胁模型

### 5.1 `AbsolutelyForbiddenInformation`

以下内容在任何公司政策和人工授权下都不得发送给 DeepSeek 或任何外部文献服务：

- 用户姓名、账号、邮箱、设备名和 Git 身份；
- 公司名称、内部项目名、内部术语和组织信息；
- IP、主机名、绝对路径、数据库地址和发布目录；
- API Key、环境变量值、令牌、密钥和高熵凭据；
- 原始行情、个股代码、个股样本和个股结果；
- 完整每日 IC、精确因子表现、完整相关矩阵和完整图谱；
- 真实因子 ID、内部公式库、账本、日志和密封样本外信息；
- 能够反推出个股、内部因子或精确历史结果的聚合数据。

### 5.2 `PolicyConditionallyExportableInformation`

下列聚合衍生信息并非天然允许外发。只有 `CorporateExternalResearchPolicy.allowed_information_classes` 逐类明确允许、外发 payload 通过扫描且用户对实际字节完成哈希授权时，才可发送：

- 通用字段别名与白名单算子能力；
- 分箱后的结构覆盖、信号覆盖和失败类型；
- 分箱后的历史表现、稳定性与市场状态差异；
- 不含内部标识和精确统计量的 coverage gap 卡片；
- 本批通用研究主题、可证伪问题和预算边界。

若政策没有列出某一信息类别，该类别默认禁止。`AbsolutelyForbiddenInformation` 不得通过政策升级为可外发。

### 5.3 外发最小化

完整图谱只在公司 Linux 服务器读取。固定程序先在本地计算覆盖摘要，外发 JSON 不包含真实因子节点或边。

对外字段使用公开语义别名：

- 真实字段名映射成 `price_close`、`traded_value`、`turnover_ratio` 等通用别名；
- 结构簇在单个 brief 内编号为 `S001`、`S002`，不输出真实簇 ID；
- 信号簇编号为 `P001`、`P002`，不输出成员因子；
- 市场状态使用通用经济标签，不输出内部 canonical ID；
- 覆盖数量使用 `0`、`1`、`2_4`、`5_plus` 分箱；
- IC 只输出 `negative`、`neutral`、`positive`、`strong_positive` 等等级；
- 稳定性和状态差异只输出冻结等级，不输出精确数值；
- 外发体不包含原始 `coverage_graph_id`，本地 manifest 单独保存映射。

### 5.4 白名单与敏感信息扫描

外发生成采用 Pydantic 白名单合同，禁止任意附加字段。规范 JSON 序列化后对完整字节再次扫描：

- IPv4、IPv6、主机名和 URL；
- Unix、Windows 和用户目录路径；
- 邮箱、账号样式和 Git 身份；
- API Key、Bearer、Token 和常见凭据前缀；
- 长高熵字符串；
- 控制字符、零宽字符和异常 Unicode；
- 由用户在服务器私有文件中维护的禁词。

私有禁词文件的路径和内容不得进入产物或日志。扫描失败必须硬失败，不提供自动删除敏感片段后继续发送的兜底。

### 5.5 公司外发政策与人工批次授权

技术脱敏不等于公司允许外发。真实 campaign 必须同时具备两层授权：

1. `CorporateExternalResearchPolicy`：由具备权限的公司负责人或公司制度提供，明确允许向指定外部供应商发送哪些聚合衍生信息；
2. `ExportAuthorization`：用户对本批实际预览字节的确认。

`CorporateExternalResearchPolicy` 至少冻结：

```text
policy_id
provider
allowed_endpoint
allowed_information_classes
forbidden_information_classes
allowed_models
maximum_authorized_campaigns
valid_from
valid_until
approver_role
approval_reference
policy_sha256
```

`approver_role` 只保存公司角色，不保存个人姓名。Factor Miner 能核验政策文档的内容、有效期和适用范围，但不能自行判断签署者是否真的拥有公司授权；部署责任人必须在服务器私有配置中提供合法政策引用。缺失、过期、范围不符或无法核验时，真实外发硬失败。

个人点击哈希授权不能替代 `CorporateExternalResearchPolicy`。

人工批次授权流程如下。

系统先发布规范化预览、预览哈希和字段统计。用户必须提交与本批 campaign、brief、模型和提示身份绑定的 `ExportAuthorization`。

授权只适用于：

- 指定 campaign；
- 指定 brief；
- 指定 DeepSeek 官方端点；
- 指定三个提示模板版本；
- 指定预算；
- 指定预览 SHA-256。

任一内容变化都使授权失效。每次调用仍重复执行敏感信息扫描。

### 5.6 剩余风险

外部 API 收到脱敏请求后，其基础设施留存、日志和内部处理不受 Factor Miner 完全控制。因此 V0.5 只能证明“固定程序未发送合同禁止的信息”，不能声称外部服务绝对零留存。

## 6. 脱敏覆盖摘要合同

### 6.1 `LLMCoverageBriefSpec`

结果生成前冻结：

```text
source_coverage_graph_id
brief_policy
field_alias_policy
performance_bucket_policy
gap_policy
algorithm_version
runtime_provenance
created_at
```

其中 `source_coverage_graph_id` 只存在于本地 build spec 和 manifest，不进入外发 payload。

### 6.2 `LLMCoverageBrief`

内容寻址 brief 至少包含：

```text
brief_id
brief_version
taxonomy_summary
structure_cluster_cards
signal_cluster_cards
coverage_gap_cards
failure_pattern_cards
regime_aliases
allowed_field_aliases
allowed_operators
allowed_windows
hypothesis_budget
candidate_budget_per_hypothesis
export_payload_sha256
```

`coverage_gap_cards` 由固定程序生成，不允许大语言模型直接从完整图谱自行寻找。每张卡片记录：

```text
gap_id
category
subcategory
field_aliases
operator_families
window_band
structure_coverage_band
signal_coverage_band
performance_direction_band
stability_band
regime_difference_band
failure_pattern_bands
```

不输出真实因子、真实公式、精确 IC、精确相关或完整成员关系。

### 6.3 冻结表现分箱

首版在 `BriefPolicy` 中冻结分箱阈值，不使用每批结果分位数动态定义：

- 平均 RankIC 绝对值：`<0.01` 为弱，`0.01–0.03` 为中等，`>0.03` 为强；
- 状态间加权平均 RankIC 最大差异：`<0.005` 为弱，`0.005–0.015` 为中等，`>0.015` 为强；
- 有效日期覆盖：`<60` 不可用，`60–99` 偏低，`100+` 充足；
- 簇规模：`1`、`2_4`、`5_plus`。

这些等级只服务覆盖摘要和隐私最小化，不构成因子准入阈值。

### 6.4 `GapSelectionPolicy`

覆盖空白不能由自然语言或 LLM 自行决定。首版先冻结有限研究单元：

```text
category
subcategory
field_family
operator_family
window_band
hypothesis_axis
```

`hypothesis_axis` 只允许 `cross_sectional`。有限单元来自版本化 `GapTaxonomySpec`，不是根据本次结果临时扩展的任意笛卡尔积。

固定程序按以下顺序生成并选择 gap：

```text
GapTaxonomySpec 中的有限单元
  → 删除字段注册表不允许真实研究的单元
  → 删除语义类型系统无法构造合法表达式的单元
  → 聚合结构节点数、独立信号簇数和不可用比例
  → 应用确定性 eligible filters
  → 应用类别探索配额
  → 按冻结 priority tuple 排序
  → canonical gap hash 稳定打破并列
  → 选出最多 10 张卡片
```

首版 eligible filters：

- 结构节点数分箱必须为 `0`、`1` 或 `2_4`；
- 独立信号簇数分箱必须为 `0`、`1` 或 `2_4`；
- 不可评价因子占比不得达到 `high_failure`；
- 所需字段必须全部通过字段 availability 注册表；
- 相同 canonical gap hash 只能出现一次。

首版排名映射固定为：

```python
count_band_rank = {"0": 0, "1": 1, "2_4": 2}
failure_risk_rank = {"none": 0, "low": 1, "medium": 2}
```

`high_failure` 在 eligible filter 阶段已经删除，不能进入排名。`category_order` 使用版本化 taxonomy 中的 canonical category ID 升序，不能依赖字典插入顺序或展示名称。单一 category 最多选择 2 张卡片。

首版选择算法精确定义为：

```python
def base_key(gap):
    return (
        count_band_rank[gap.structure_count_band],
        count_band_rank[gap.signal_cluster_count_band],
        failure_risk_rank[gap.failure_risk],
        gap.canonical_gap_hash,
    )

selected = []
remaining = deduplicated_eligible_gaps

# 第一轮：预算允许时，每个有 eligible gap 的 category 先取一个。
for category in sorted(unique_categories(remaining)):
    if len(selected) == 10:
        break
    candidates = [g for g in remaining if g.category == category]
    chosen = min(candidates, key=base_key)
    selected.append(chosen)
    remaining.remove(chosen)

# 第二轮：每次选择后重新计算动态类别占用数。
while len(selected) < 10:
    allocation = count_selected_by_category(selected)
    candidates = [
        g for g in remaining
        if allocation.get(g.category, 0) < 2
    ]
    if not candidates:
        break
    chosen = min(
        candidates,
        key=lambda g: (
            count_band_rank[g.structure_count_band],
            count_band_rank[g.signal_cluster_count_band],
            failure_risk_rank[g.failure_risk],
            allocation.get(g.category, 0),
            category_order.index(g.category),
            g.canonical_gap_hash,
        ),
    )
    selected.append(chosen)
    remaining.remove(chosen)
```

表现方向、IC 强弱和 regime 差异不得进入 gap 排序，避免直接追逐历史表现；它们仅作为选中卡片的 outcome-informed 注释提供给主 LLM arm。表现弱但覆盖多不属于 gap，高失败集中区域属于数据或定义风险，不因“稀疏”获得优先级。regime 差异既不奖励也不惩罚，只标记为探索性上下文。

每张卡片保存所有过滤、配额和排序分量，使“为什么研究这十个方向”可以逐字节重放。

## 7. 发现研究族、批次与多重检验

### 7.1 `LLMDiscoveryResearchFamilySpec`

V0.5 在现有候选级 `ResearchFamilySpec` 上方增加候选生成前的发现研究族。它在任何外部调用或 outcome 暴露前冻结：

```text
discovery_context_data_identity
candidate_evaluation_data_identity
evaluation_relationship
coverage_graph_ids
hypothesis_origin
arm_specs
maximum_llm_campaigns
arm_run_count
candidate_slots_per_arm
global_statistical_trial_budget
multiplicity_policy
outcome_informed_decision_budget
family_max_api_requests
family_max_literature_queries
family_max_total_input_tokens
family_max_total_output_tokens
evaluation_policy_id
created_at
```

`discovery_context_data_identity` 必须与生成 V0.4 图谱所用的数据发布、股票池、标签、可见起止日、状态快照、参考因子库和评价政策逐项一致，并记录 `latest_information_timestamp_used`，覆盖形成图谱时用到的特征、标签和派生统计的最晚信息时点。`candidate_evaluation_data_identity` 单独绑定候选评价所用的同类身份。`evaluation_relationship` 只能为：

- `strict_temporal_holdout`：首个候选形成时点严格晚于 discovery context 的 `latest_information_timestamp_used`，特征与标签所依赖的原始观测均不重叠，且候选在评价器读取该期间任何数据前已经封存；
- `reused_discovery_data`：评价数据与图谱数据存在任何重叠，只能形成探索性筛选结果。

严格时间留出仍不是密封样本外认证，但它避免用同一段表现先选方向、再把普通 Bonferroni p 值解释为完整选择后推断。若复用 discovery 数据，HAC 与 120 槽 Bonferroni 只控制冻结候选族内部的名义检验，不能校正图谱驱动的上游选择偏差；相应字段必须命名为 `exploratory_family_adjusted_p`，不得输出“统计通过”或“支持”。

首版 family 在 outcome 前一次性冻结四个同预算 arm：

| arm | 输入与生成方式 | 候选槽 |
|---|---|---:|
| `coverage_outcome_llm` | 覆盖 gap、分箱表现、状态差异和失败模式 | 30 |
| `literature_only_llm` | 同一字段与 DSL 边界，但不提供覆盖图谱和历史表现 | 30 |
| `mechanical_mutation` | 固定程序对已有合法结构做预登记机械变异 | 30 |
| `hypothesis_conditioned_grammar` | 固定种子、受假设约束的类型化 grammar 采样 | 30 |

因此首版：

```text
maximum_llm_campaigns = 2
arm_run_count = 4
candidate_slots_per_arm = 30
global_statistical_trial_budget = 120
multiplicity_policy = bonferroni_over_frozen_family_budget
family_max_api_requests = 90
family_max_literature_queries = 40
family_max_total_input_tokens = 2000000
family_max_total_output_tokens = 400000
```

四个 arm 的候选使用同一 candidate evaluation 身份、相同单候选计算协议和同一 120 槽 Bonferroni family。只有两个 LLM arm 创建 campaign；另外两个是确定性 `arm_run`。任何一个 arm 的失败、低质量或未执行槽都不能在结果后缩小全局统计预算。

`coverage_outcome_llm` 的假设固定标记：

```text
hypothesis_origin = coverage_outcome_informed
research_mode = discovery
```

它不能被称为纯理论先验或 confirmatory hypothesis。`literature_only_llm` 也只是 discovery 对照，不因未读取图谱而自动成为确认性证据。

同一 discovery context 上需要增加 LLM campaign 或 arm run 时，必须在 family seal 前修改并重新冻结 family；seal 后不可扩展。新建 campaign ID 不能重置统计 family。

候选生成后，固定程序把 120 个预登记 discovery slot 映射成现有候选级 `ResearchFamilySpec`，其 `global_hypothesis_budget` 必须等于 120，供现有 HAC、Bonferroni 和 V0.2 流程使用。

由追加事件确定性投影的 `LLMDiscoveryFamilyState` 保存：

```text
discovery_family_id
discovery_context_data_identity
candidate_evaluation_data_identity
evaluation_relationship
coverage_graph_ids
all_llm_campaign_ids
all_arm_run_ids
family_generation_state
generation_seal_id
generation_seal_sha256
cumulative_generated_candidates
cumulative_evaluated_candidates
cumulative_outcome_informed_decisions
global_statistical_trial_budget
multiplicity_policy
outcome_exposed
family_closed_at
```

它是事件投影，不允许人工编辑。`family_closed_at` 只由封存或关闭事件投影，不能写进不可变 spec。`generation_sealed` 与 `family_closed_at` 一旦出现就不能重新打开；`outcome_exposed=true` 后禁止新增或替换 arm、campaign 和 slot。

### 7.2 `LLMResearchCampaignSpec`

每个 LLM arm 的批次登记前冻结：

```text
discovery_family_id
arm_id
brief_id
corporate_external_research_policy_id
provider
base_url
model
thinking
reasoning_effort
prompt_bundle_id
literature_policy_id
hypothesis_slots
candidate_slots_per_hypothesis
call_topology
max_api_requests
max_literature_queries
max_total_input_tokens
max_total_output_tokens
repair_policy
created_at
```

单个 LLM arm 固定：

```text
provider = deepseek
base_url = https://api.deepseek.com
model = deepseek-v4-pro
thinking = enabled
reasoning_effort = high
hypothesis_slots = 10
candidate_slots_per_hypothesis = 3
max_api_requests = 45
max_literature_queries = 20
max_total_input_tokens = 1000000
max_total_output_tokens = 200000
```

base URL 不允许被环境变量、CLI 或输入 JSON 覆盖。机械变异与 hypothesis-conditioned grammar arm 不创建虚假的 DeepSeek campaign，只登记相同 30 槽的确定性 generator spec。

### 7.3 槽位、调用、统计与结果状态不得混用

每个槽和调用分别记录：

```text
research_slot_consumed
generation_attempt_consumed
api_budget_consumed
statistical_trial_counted
outcome_exposed
```

研究拒绝、重复、非法公式和人工否决会保留生成槽，禁止为了获得更漂亮输出而补抽。供应商 500、网络中断、磁盘写入失败和已确认的程序缺陷属于基础设施失败，可以按冻结恢复政策重试，不产生新的研究想法。

无论基础设施失败多少，统计推断始终使用 outcome 前冻结的 family budget 120，因此恢复不会缩小多重检验分母。若基础设施失败发生在模型已经返回语义内容之后，原响应必须保留，不能重试到另一个更满意的研究想法。

### 7.4 两个独立 benchmark estimand

四个 arm 不做一张含义混杂的总排名，而回答两个不同问题。

**对比 A：假设上下文的增量价值**

`coverage_outcome_llm` 对比 `literature_only_llm`。两者使用相同模型、代理 2 表达式生成器、semantic lint、字段/DSL 边界和 10×3 预留预算，唯一核心差异是代理 1 是否获得脱敏覆盖图谱结果上下文。

- 假设层分母：每 arm 固定 10 个 hypothesis slot；
- 候选层主分母：每 arm 固定 30 个 candidate slot；
- 另报告“每个获批假设”和“每个生成成功候选”的条件指标，但不能取代固定槽分母；
- 比较人工假设接受率、来源支持结构、候选合法率，以及封存后评价结果。

**对比 B：表达式生成器的增量价值**

在 `coverage_outcome_llm` 同一组人工批准并冻结的 coverage hypothesis 上，比较：

1. LLM 代理 2；
2. `mechanical_mutation`；
3. `hypothesis_conditioned_grammar`。

三者必须共享同一 hypothesis、每假设 3 槽、字段族、算子族、窗口带、方向、单位、availability 和未声明暴露限制。grammar 不是无条件随机公式，避免把过弱基线当成对照。

- 固定槽分母：每 arm 30；
- matched-eligible 分母：`3 × 获批 coverage hypothesis 数`；
- 两套分母同时报告，未获批假设对应槽记录为未执行；
- 机械与 grammar arm 不适用人工假设接受率和文献支持率，不得把“不适用”计成零。

### 7.5 Shadow generator 合同

`literature_only_llm` 使用相同 DeepSeek 模型、三代理流程、人工闸门、字段/DSL 边界和 10×3 预算，但输入只含公开字段能力、文献工具和有限经济主题 taxonomy，不含 V0.4 节点、gap、表现或失败摘要。其 origin 为 `literature_informed`。

`mechanical_mutation` 和 `hypothesis_conditioned_grammar` 不编造独立经济故事。它们复用 `coverage_outcome_llm` 经人工批准的同一组 `RegisteredCoverageGapHypothesis` 与 `TestablePredictionSpec`，每个获批假设各分配 3 个表达式槽，从而只比较表达式生成方法。未获批假设对应的 baseline 槽保持未执行，但继续留在 120 槽统计 family。

`mechanical_mutation` 在 outcome 前冻结：

```text
parent_ast_ids
mutation_operators
mutation_order
window_substitution_order
maximum_mutations_per_parent
deduplication_policy
slot_assignment_order
```

只允许单步算子替换、窗口替换和合法子树组合。父 AST 顺序和变异枚举顺序确定性固定；跳过非法变异后取前 30 个槽，不根据 IC 或人工偏好选择。origin 为 `mechanical_mutation`。

每个 hypothesis 的 parent AST 必须先由固定程序按该假设冻结的字段族、算子族、窗口带、方向、单位和 availability 约束筛选；没有合格 parent 时对应槽进入明确未执行终态，不得借用不匹配父公式。

`hypothesis_conditioned_grammar` 在 outcome 前冻结：

```text
grammar_version
allowed_field_families_by_hypothesis
allowed_operator_families_by_hypothesis
allowed_window_bands_by_hypothesis
expected_direction_by_hypothesis
unit_and_availability_constraints
depth_distribution
node_distribution
random_seed
maximum_draws
deduplication_policy
slot_assignment_order
```

随机生成器每次 draw 都记录；非法或重复 draw 消耗 generation attempt，但固定程序继续抽取直到填满 30 个预登记槽或达到 `maximum_draws`。它不能读取图谱表现或候选 outcome。origin 为 `hypothesis_conditioned_grammar`。

四个 arm 的 parent lineage、生成尝试和最终槽位都进入同一 discovery family manifest。

### 7.6 不可逆生成封存与结果防火墙

`family_generation_state` 只允许：

```text
registered
generating
generation_sealed
evaluation_open
evaluated
```

只有 120 个预登记 candidate slot 全部进入不可变生成终态，固定程序才能追加 `generation_sealed` 事件。终态包括成功候选、各类校验失败、重复、人工拒绝导致的未执行和基础设施终止；任何槽缺失都不得封存。

封存 manifest 至少哈希：

- family spec、四个 arm run spec 与两个 LLM campaign spec；
- 120 槽位映射及每槽终态；
- 所有候选 spec、失败记录和未执行记录；
- prompt、模型、生成器、随机种子、调用记录与人工决策；
- 两个数据身份、评价关系、评价政策和 120 槽多重检验预算。

`generation_sealed` 不可撤销。seal 后禁止补抽、替换、删除或更改任何槽；纠错只能创建新的 discovery family。评价器和任何 outcome 数据端口必须接收并核验 `generation_seal_id` 与 `generation_seal_sha256`，在核验前不得打开标签、IC、p 值、冗余、残差增量或候选结果文件。由此保证“先把全部研究选择冻结，再看结果”。

## 8. 三个代理的最小权限

### 8.1 代理 1：经济学假设代理

输入：

- 脱敏 `LLMCoverageBrief`；
- 冻结的 10 个假设槽；
- 金融假设 JSON Schema；
- 引用和可证伪要求；
- 受限文献检索工具定义。

不得输入：

- 真实因子 ID、公式、完整图谱或精确 IC；
- 标签、结果工具和现有候选评价结果；
- 服务器、文件、终端或任意 URL 工具。

输出 `CoverageGapHypothesisDraft`：

```text
slot_id
gap_id
hypothesis_origin
claim
economic_mechanism
observable_proxy
independent_verification
competing_explanations
failure_modes
falsification_path
source_record_ids
proposed_field_aliases
proposed_operator_families
prediction_proposal
```

模型不得把文献相关性描述成已验证因果机制。

### 8.2 `PredictionProposal` 与 `TestablePredictionSpec`

LLM 只能在 `PredictionProposal` 中提出：

```text
observable_proxy
expected_sign
proposed_field_aliases
proposed_operator_families
optional_conditioning_claim
```

它不得输出评价政策、股票池、形成时点、预测起点、期限、alpha、多重检验 family、最低样本、覆盖率、支持规则、证伪规则或 robustness 列表；出现这些额外字段时 Schema 直接拒绝。

固定程序 `compose_testable_prediction()` 把提案、冻结 `EvaluationPolicySpec` 和 discovery family 合成为机器可执行的 `TestablePredictionSpec`：

```text
evaluation_policy_id
universe_id
applicable_market
hypothesis_axis
formation_time
prediction_start
horizon_sessions
conditioning_variables
effect_definition
expected_sign
primary_metric
null_hypothesis
minimum_effect_size
alpha
multiplicity_family_id
minimum_valid_dates
minimum_names_per_date
minimum_median_coverage
support_rule
falsification_rule
inconclusive_rule
allowed_robustness_checks
```

首版只允许：

```text
hypothesis_axis = cross_sectional
primary_metric = mean_daily_rank_ic
effect_definition = daily_cross_sectional_spearman_factor_vs_o2o_5d
null_hypothesis = oriented_mean_rank_ic_le_zero
prediction_start = open_t_plus_1
horizon_sessions = 5
minimum_effect_size = 0.01
conditioning_variables = ()
allowed_robustness_checks = (
  annual_rank_ic_summary,
  regime_weighted_rank_ic_summary,
  raw_vs_residual_rank_ic_comparison
)
```

股票池、形成时点、预测起点、期限、alpha、最低样本、覆盖率和判定规则全部由固定程序注入。若 LLM 提议的方向、字段、算子或 conditioning claim 不能映射到冻结政策，草案失败，不允许程序猜测或悄悄放宽。

V0.3 regime 只能出现在预登记 robustness check，首版不能作为主检验条件或结果后选择的子样本。若未来要把某个状态条件效应作为主张，必须占用独立候选槽并扩展评价政策与多重检验 family。

结果状态由固定程序产生：

- 当 `evaluation_relationship=strict_temporal_holdout`：
  - `supported_on_fresh_visible_validation`：方向正确、效果量达标、完整 family Bonferroni 显著、覆盖率通过，且冗余与残差增量闸门通过；
  - `falsified_on_fresh_visible_validation`：样本和覆盖率通过，方向对齐后的效果显著落在不低于最小效果量的反方向；
  - `inconclusive_on_fresh_visible_validation`：其余情形，包括效果弱、统计功效不足、覆盖不足或结果不稳定。
- 当 `evaluation_relationship=reused_discovery_data`：
  - `passes_exploratory_filter`；
  - `fails_exploratory_filter`；
  - `inconclusive_exploratory`。

这些都不是密封样本外确认；后一组尤其不得解释为控制了图谱选择偏差。三个 robustness check 只作描述性诊断，不改变主状态。任何额外期限、股票池、状态条件、预处理或规格都产生新候选槽，不得结果后选择最有利规格。

候选结果与假设汇总严格分层。`HypothesisDiscoverySummary` 只能使用：

```text
no_valid_candidate
has_supported_candidate
mixed_candidate_evidence
all_candidates_inconclusive
all_valid_candidates_reverse
```

复用 discovery 数据时不得使用 `has_supported_candidate`，只另记候选探索性筛选计数。任何汇总都不得称为 `hypothesis_supported` 或 `mechanism_falsified`；候选因子表现不能单独证明或推翻经济机制。

严格时间留出下的摘要优先级固定为：

1. 没有进入有效评价的候选：`no_valid_candidate`；
2. 全部有效候选均 inconclusive：`all_candidates_inconclusive`；
3. 全部有效候选均 reverse：`all_valid_candidates_reverse`；
4. 同时出现 supported 与 reverse，或 reverse 与 inconclusive：`mixed_candidate_evidence`；
5. 至少一个 supported 且没有 reverse：`has_supported_candidate`。

### 8.3 人工筛选与冻结

人工决定使用独立 `HypothesisDecision`：

```text
draft_id
decision
reason
verified_source_record_ids
claim_support_records
corrections
reviewer
created_at
```

`reviewer` 使用本地匿名角色 ID，不保存姓名或账号。更正不覆盖草案，而是生成带 `supersedes_id` 的新事件。

只有固定程序成功合成 `TestablePredictionSpec`、来源状态满足第 9 节对应通道且人工批准的草案，才可以生成不可变 `RegisteredCoverageGapHypothesis`。机制状态固定为 `mechanism_unverified`。

### 8.4 代理 2：数学表达式代理

每次只处理一个获批假设，输入：

- 冻结假设；
- 公开字段别名；
- 白名单算子、窗口和 typed AST Schema；
- 3 个预留候选槽。

不得输入覆盖图谱、IC、状态画像、标签或其他假设结果。输出只能是 typed AST JSON 和槽位 ID，禁止 Python、源码、动态算子和任意属性访问。

固定程序将公开字段别名映射回本地真实字段，并依次执行：

1. Pydantic Schema；
2. 字段 availability 注册表；
3. 单位、轴、shape 和算子语义类型；
4. 字段与算子白名单；
5. 最大节点、深度和 lookback；
6. negative shift、centered rolling、future field 和标签泄漏检查；
7. 规范 AST 与内容哈希；
8. 确定性 `ExposureSignature`；
9. 与现有参考库和本批更早槽位的结构重复检查；
10. 确定性编译计划。

`ExposureSignature` 只做机器可以可靠完成的结构暴露分析：

```text
field_families
operator_families
window_bands
unit_dimension
axis_type
lag_structure
availability_bound
```

它必须是冻结假设声明字段、算子、窗口和时点的子集。固定程序不尝试从公式“证明经济机制”，这仍由人工假设审查和无证据权重的 semantic lint 处理。

任何失败都发生在 outcome 端口打开前。

### 8.5 代理 3：LLM semantic lint

每次只读取：

- 一个冻结假设；
- 一个已通过固定程序编译的公开别名 AST；
- 冻结审查量表。

审查四个维度：

1. observable proxy 是否对应；
2. 预期方向是否一致；
3. 信息可得时点是否一致；
4. 公式是否引入假设未声明的经济暴露。

输出：

```text
candidate_slot_id
decision
proxy_alignment
direction_alignment
availability_alignment
undeclared_exposure
reason_codes
summary
```

代理 3 只能批准或拒绝，不能修改假设、AST、预算或评价政策。它与代理 2 使用同一模型家族，错误并不独立，因此只称为 `LLM semantic lint`，不具有证据权重，也不是外部验证或认证。

真正的硬 gate 是字段 availability 推导、语义类型、确定性 exposure analysis、DSL/编译器和人工批准。semantic lint 的拒绝保留记录；其批准不能覆盖任何固定程序失败。

## 9. 受限文献检索网关

### 9.1 网络边界

只有代理 1 可以通过固定 Tool Layer 请求文献检索。大语言模型不直接访问网络。

首版批准的元数据适配器：

- Crossref；
- OpenAlex；
- arXiv。

SSRN、NBER 和期刊论文只有在上述公开元数据中具备稳定标识时才进入记录。首版不抓取任意网页正文，不跟随模型提供的 URL，不访问 RFC1918、localhost、链接本地地址或非批准域名。

### 9.2 查询最小化

工具只接受：

```text
query_terms
year_start
year_end
result_limit
```

查询词必须来自 brief 的公开字段别名、经济概念和模型生成的通用机制词。固定程序再次运行敏感信息扫描。单次结果数和文本长度受限，campaign 总查询数不超过 20。

### 9.3 来源核验

`LiteratureSearchRecord` 保存：

```text
provider
normalized_query
retrieved_at
results
response_sha256
```

每个结果只保留：

```text
public_identifier
title
authors
publication_year
abstract_excerpt
source_name
canonical_url
metadata_sha256
verification_status
```

DOI 必须由元数据端点返回并完成规范化；arXiv 使用稳定 arXiv ID。来源核验严格拆成三层：

```text
identity_verified
passage_verified
claim_support_verified
```

- `identity_verified` 只表示标题、作者、年份和公开标识一致；
- `passage_verified` 要求人工保存页码、章节、表格或公式定位，以及最短必要原文片段；
- `claim_support_verified` 还要记录该段支持的是机制、代理选择还是研究背景，以及适用市场与样本。

来源技术核验与假设的文献状态分开记录。单条 `CitationSupportRecord.support_kind` 只能为：

```text
mechanism_claim_supported
proxy_choice_supported
background_only
```

假设级 `HypothesisLiteratureStatus` 才允许：

```text
mechanism_claim_supported
proxy_choice_supported
background_only
novel_unverified
```

`mechanism_claim_supported` 不能由只谈相关性的段落产生；`proxy_choice_supported` 只支持可观察代理，不支持完整经济机制；`background_only` 只能提供背景。若 V0.5 只能访问摘要，则最高技术状态为 `abstract_consistent`，不能冒充 passage 或 claim support。

假设状态由其全部 citation records 确定性汇总，不允许模型自报更高等级：存在合格机制支持时为 `mechanism_claim_supported`；否则存在合格代理支持时为 `proxy_choice_supported`；否则只有背景来源时先为 `background_only`。`background_only` 不能直接通过普通文献闸门。

V0.5 明确保留一个小规模新颖性通道：人工可以把 `background_only` 草案通过独立事件批准为 `novel_unverified`，每个 10 槽 LLM arm 最多 2 个。这类假设仍须有可核验的背景来源、完整证伪路径和明确批准理由，但不得声称核心机制获文献支持，机制状态仍为 `mechanism_unverified`。超出配额、没有任何可核验来源或把背景文献包装成机制支持时，草案不能进入表达式生成。

`CitationSupportRecord` 至少保存：

```text
source_record_id
identity_status
passage_locator
minimal_excerpt
support_kind
supported_claim_fragment
applicable_market
applicable_sample
counterevidence_search_performed
counterevidence_queries
counterevidence_source_ids
counterevidence_cutoff
counterevidence_summary
counterevidence_limitations
reviewer_role
created_at
```

`counterevidence_search_performed=true` 只表示执行了冻结查询协议，不表示已经穷尽反例。查询词、来源、截止日和局限必须同时保存；不得使用含义过强的 `counterevidence_checked`。

文献元数据和人工摘录都属于不可信输入。固定程序去除 HTML、限制长度、拒绝控制字符和异常 Unicode，并在提示中把它明确包裹为“不可执行的数据”。文献内容不得改变系统提示、工具权限或输出 Schema。

服务不可达或限流时不得改用通用搜索引擎兜底。

## 10. DeepSeek 调用合同

### 10.1 固定请求

使用 DeepSeek 官方 OpenAI 兼容 Chat Completions 接口：

```text
base_url = https://api.deepseek.com
model = deepseek-v4-pro
thinking.type = enabled
reasoning_effort = high
stream = false
response_format.type = json_object
```

thinking 模式不发送无效的 `temperature`、`top_p`、`presence_penalty` 或 `frequency_penalty` 参数。

使用 OpenAI Python SDK 时必须通过：

```text
extra_body = {"thinking": {"type": "enabled"}}
```

传递 thinking 开关。每个 system prompt 和 user prompt 都必须显式要求“输出 JSON object”，不能只依赖 `response_format`，避免官方文档警告的连续空白输出。

API Key 只从 `DEEPSEEK_API_KEY` 读取。缺失时只允许 brief、预览、录制重放和离线验证，真实调用必须失败。密钥不得进入异常文本、日志、request record、环境快照或子进程参数。

### 10.2 无工具代理

代理 2 和代理 3 的请求不提供 tools，模型无法调用任何函数。

### 10.3 文献工具循环

代理 1 的工具循环由固定程序控制：

- 只注册一个受限 `search_literature` 工具；
- 不发送 `tool_choice`；
- 每次工具调用先校验参数、预算和隐私；
- 工具调用返回后按 DeepSeek 合同回传必要的 `reasoning_content`；
- 最终完成后不保存 `reasoning_content`；
- 超过工具轮数或查询预算立即失败。

### 10.4 输出验证

模型最终 `content` 必须是 JSON object，并满足对应 Pydantic Schema。未知字段、额外槽位、重复槽位、非有限数值、自由 Python 或未知字段别名都失败关闭。

### 10.5 冻结调用拓扑

单个 10×3 LLM arm 的最坏调用拓扑：

| 调用类型 | 上限 |
|---|---:|
| 假设代理初始、最多 8 个工具轮次和最终输出 | 10 |
| 表达式代理，每个假设批量生成 3 个 | 10 |
| semantic lint，每个假设批量审查 3 个 | 10 |
| 格式修复储备 | 5 |
| 传输重试储备 | 10 |
| HTTP 请求总计 | 45 |

代理 3 不逐候选单独调用。单次工具轮次可以包含多个经过校验的文献查询，但 campaign 的公开元数据查询总数仍不超过 20。调用拓扑、批量边界和储备在 campaign 登记后不可改变。

角色级输出预留：

| 请求类型 | 单次 `max_tokens` 上限 |
|---|---:|
| 假设代理与工具轮次 | 8,000 |
| 每假设 3 个表达式 | 6,000 |
| 每假设 3 个 semantic lint | 4,000 |
| 格式修复 | 4,000 |

不含传输重试时最坏输出预留为 200,000 token。重试也必须重新取得预算预留，因此达到总输出上限后即使仍有重试次数也不得发送。

## 11. 重试、修复和费用控制

### 11.1 传输重试

网络超时、HTTP 429、5xx 和 `finish_reason=insufficient_system_resource` 可以使用最多 10 次全 campaign 传输重试储备，单个逻辑调用最多重试 2 次。每次尝试都写独立 `LLMCallAttempt`，不覆盖前次状态。

### 11.2 格式修复

以下情形允许一次预先定义的格式修复调用：

- 最终 content 为空；
- `finish_reason=length`；
- JSON 无法解析；
- JSON 不满足 Schema。

修复提示只包含原请求身份、原始脱敏 content 和机器生成的 Schema 错误，不添加研究反馈、不更换模型、不改变预算。修复仍失败则原槽进入 `generation_failed`。

内容过滤、隐私扫描失败、未授权导出、工具越权、密钥异常或预算超限不得修复或重试。

### 11.3 费用与 token 闸门

每次请求前先进行保守预算预留：

```text
estimated_input_tokens <= remaining_input_budget
request.max_tokens <= remaining_output_budget
```

DeepSeek 未提供可离线保证完全一致的 tokenizer 时，输入预估使用规范请求 UTF-8 字节数作为 token 数的保守上界。输出预算在请求发出前按 `max_tokens` 预留，不等待响应后才判断超额。角色级 `max_tokens` 等于角色固定上限与剩余输出预算的较小值。

每次响应后使用官方 usage 对账并保存差异。传输重试、格式修复、工具循环重复发送的历史上下文和官方计入 completion usage 的 reasoning token 都计入实际 API 预算。HTTP 失败但供应商可能计费时，若没有可信 usage，则保留已做的预算预留，不返还。

达到任一上限后停止：

- 45 次 API 请求；
- 20 次文献查询；
- 1,000,000 输入 token；
- 200,000 输出 token。

服务价格会变化，因此正式合同用 token 和请求次数控制，不把易变价格写成安全闸门。调用记录可保存当时的非约束性费用估算。

## 12. 可复现性与审计

外部大语言模型不能保证相同输入生成相同输出。V0.5 的可复现定义是“冻结并重放已经观察到的请求和响应”，不是声称重新调用必然相同。

每次 `LLMCallRecord` 保存：

```text
campaign_id
agent_role
slot_ids
request_sha256
response_sha256
model
system_fingerprint
finish_reason
usage
started_at
completed_at
attempt_ids
status
```

本地独立保存已经通过隐私扫描的规范请求和最终 JSON 响应。隐藏思维链不进入正式产物。录制重放必须逐字读取原响应，并再次执行当前 Schema 和哈希核验；不得重新访问 DeepSeek 或文献服务。

模型名、系统指纹、提示、工具合同或响应变化都产生新记录。基础设施恢复使用同一逻辑调用的幂等键；模型已经返回语义内容后不得在同一槽重新生成。真正的新研究生成只能使用 discovery family 在 outcome 前预留的 campaign/slot，不能覆盖、伪装成重放或另建 family 重置统计预算。

## 13. 状态机与文件账本

V0.5 复用现有 `JsonlLedger` 已验证的单写入者机制：非阻塞文件锁、连续 sequence、previous hash、规范 JSON、append 后 flush/fsync，以及不可变文档的临时文件、fsync 和原子 rename。V0.5 可以使用独立事件类型和独立 ledger 文件，但不得另写一个缺少这些不变量的简化账本。

campaign 只保存汇总状态，部分成功由对象级状态机表达。campaign 汇总状态：

```text
registered
brief_verified
export_authorized
in_progress
awaiting_human_review
packaged_with_partial_results
operationally_completed
operationally_failed
```

生成质量是独立的 `CampaignQualityAssessment`，字段为 `not_assessed`、`passed` 或 `failed`，不能冒充 campaign 生命周期状态。一个 operationally completed campaign 可以同时有 failed quality assessment。

hypothesis slot：

```text
reserved
generation_in_progress
draft_generated
empty
source_unverified
awaiting_human_review
human_rejected
human_approved
frozen
generation_failed
not_executed
```

candidate slot：

```text
reserved
generation_in_progress
draft_generated
schema_failed
semantic_type_failed
availability_failed
dsl_failed
lookahead_failed
duplicate_failed
compile_failed
semantic_lint_rejected
generation_failed
not_executed_hypothesis_rejected
not_executed_infrastructure_terminal
ready_for_registration
```

LLM logical call：

```text
reserved
request_persisted
in_flight
transport_retryable
repairable
response_persisted
validated
terminal_failed
```

literature query：

```text
reserved
in_flight
identity_verified
abstract_consistent
passage_verified
claim_support_verified
source_unverified
transport_retryable
terminal_failed
```

export authorization：

```text
previewed
corporate_policy_verified
user_authorized
active
expired
invalidated
```

每条状态转换都有显式允许边。`operationally_completed` 表示所有预留槽已进入某个终态，不要求所有槽成功；`operationally_failed` 表示协议无法继续。质量通过与否由独立 assessment 表达。对象失败不强迫整个 campaign 进入单一 `failed`。

每个外部请求在发送前先原子保存 request 和幂等键，再写 `in_flight` 事件。进程崩溃后：

- 没有已保存 request 的槽回到上一个稳定状态；
- 已保存 request 但无响应时，只能按同一幂等键执行传输恢复；
- 已保存响应时不得重新调用模型，必须从响应继续验证；
- 已发布对象终态时重复命令必须返回原结果；
- 无法判断供应商是否已经接收请求时保留预算预留，并要求显式恢复命令。

`llm_events.jsonl` 只允许追加。纠错使用 `supersedes_event_id`。检测到并发写入、重复 event ID、幂等键冲突、哈希链断裂、截断、非法状态转换或状态逆转时硬失败。

正式产物建议结构：

```text
artifacts/
  llm_discovery_families/<discovery_family_id>/
    family_spec.json
    arm_specs/
    family_events.jsonl
    benchmark_summary.json
  llm_briefs/<brief_id>/
  llm_campaigns/<campaign_id>/
    campaign_spec.json
    corporate_policy_reference.json
    export_preview.json
    export_authorization.json
    llm_events.jsonl
    literature/
    calls/
    hypothesis_drafts.jsonl
    hypothesis_decisions.jsonl
    registered_hypotheses/
    candidate_drafts.jsonl
    consistency_reviews.jsonl
    candidate_specs/
    manifest.json
```

候选包进入现有登记流程前仍只是结果产物，不得命名为 Evidence 或 Conclusion。

## 14. CLI

正式入口：

```bash
factor-miner llm brief-build <coverage_graph> --artifact-root <root>
factor-miner llm brief-verify <brief>
factor-miner llm family-register <discovery_family_spec>
factor-miner llm campaign-register <campaign_spec>
factor-miner llm export-preview <campaign_id>
factor-miner llm authorize-export <campaign_spec> <preview_sha256>
factor-miner llm generate-hypotheses <campaign_spec>
factor-miner llm approve-hypotheses <decision_json>
factor-miner llm generate-candidates <campaign_id>
factor-miner llm generate-baseline <family_id> <arm_id>
factor-miner llm family-seal <family_id>
factor-miner llm evaluation-open <family_id> <generation_seal_id>
factor-miner llm recover <campaign_id>
factor-miner llm campaign-verify <campaign_id>
factor-miner llm family-verify <family_id>
```

`brief-build` 读取完整真实图谱，因此真实模式只允许公司 Linux。Mac 只能使用程序生成的合成图谱。

`family-register` 必须在任何外部调用和 outcome 暴露前冻结四个 benchmark arm、两个数据身份与 120 槽统计预算。`campaign-register` 只能占用 family 预留的 LLM arm。

`family-seal` 只有在 120 个槽全部终结且 manifest 一致时成功。`evaluation-open` 必须核验 seal、两个数据身份和预先冻结的 `evaluation_relationship`；登记为严格时间留出但实际不成立时硬失败，不允许自动降级或由 CLI 参数强行声称独立。需要复用 discovery 数据时，必须在 outcome 前把新 family 明确登记为 `reused_discovery_data`。

`generate-hypotheses` 和 `generate-candidates` 在缺少公司外发政策、用户授权、密钥、网络政策、预算或完整前置状态时失败。`recover` 只能按已保存 request、response 和幂等键恢复基础设施中断，不能重新生成语义内容。CLI 不提供跳过隐私扫描、替换模型、增加预算、忽略来源、自动批准、重置槽位、缩小 family、绕过 seal 或重新抽样开关。

## 15. 与现有候选协议衔接

### 15.1 `FieldAvailabilityRegistry`

候选不能统一假定“收盘后可得、下一交易日开盘交易”。服务器必须提供版本化字段注册表：

```text
registry_id
data_release_id
field_id
public_alias
economic_type
unit_dimension
panel_shape
event_time
source_publish_time
vendor_available_time
revision_policy
point_in_time_guarantee
earliest_decision_time
eligible_for_factor
```

财务字段必须绑定公告时间和供应商入库时间；流通股本、换手率、停牌和涨跌停状态必须绑定 point-in-time 版本；可被未来公司行为重写的后复权历史字段若没有 point-in-time 保证，`eligible_for_factor=false`。

AST availability analyzer 对所有叶节点和依赖执行确定性上界推导：

```text
candidate_earliest_decision_time
  = max(all_dependency_available_times)
candidate_earliest_trade
  = first_allowed_trade_after(candidate_earliest_decision_time)
```

首版即使叶字段位于 `delay` 下也使用当前形成日 availability 上界，宁可保守延后，不允许乐观推断。候选不得覆盖推导结果。评价 policy 的最早交易时间只能等于或晚于候选推导结果。

为保持现有 V0.1 `AvailabilitySpec` 内容身份不变，V0.5 首版只允许推导结果不晚于 `after_close_t` 决策、`open_t_plus_1` 最早交易的字段进入生成白名单。需要更晚时点的候选明确进入 `availability_failed`，不把新时点悄悄塞进旧 Schema；支持更多时点时另增版本化 candidate spec。

### 15.2 DSL 语义类型

现有 V0 DSL 已冻结算子白名单、节点/深度/lookback、完整 rolling 窗口、除零返回 null、缺失保留和禁止未来引用，但尚未实现完整单位/轴类型。V0.5 在称其为 typed AST 前必须新增版本化 `DslSemanticTypePolicy`：

- `unit_dimension`：价格、收益率、股数、货币成交额、比率和无量纲；
- `axis_type`：单资产时间序列或截面；首版表达式层只允许单资产时间序列，截面处理仍属于评价层；
- `panel_shape`：按 `asset/date` 的标量面板；
- `add/sub` 要求单位相容；
- `mul/div` 确定性组合单位，分母使用既有安全除法；
- `neg/abs/delay/delta/rolling_mean/std/min/max/sum` 按冻结规则传播单位与轴；
- `rolling_corr` 要求两个时间序列并输出无量纲；
- rolling `min_periods` 固定等于完整窗口；
- lookback 继续由完整 AST 推导；
- NaN、Inf、零除和空值继续使用既有 raw factor 政策；
- 停牌、ST、上市初期、涨跌停和交易资格继续由数据合同与评价 mask 决定，不由表达式填充。

例如 `price_close + traded_value` 必须语义类型失败。DSL 不支持的 `rank_ts` 或任意截面算子必须继续拒绝，不能因为模型输出了名字就动态增加。

### 15.3 `TrustedCandidateFactorSpec` 转换

获批且一致性通过的候选转换为现有 `TrustedCandidateFactorSpec`：

- hypothesis 来自 `RegisteredCoverageGapHypothesis`；
- expression 来自已通过固定编译的 typed AST；
- required_fields 由 AST 依赖确定，不接受模型自由声明；
- max_lookback 由编译器推导；
- availability 由 `FieldAvailabilityRegistry` 和 AST 确定性推导；
- provenance 记录 discovery family、benchmark arm、brief、campaign、假设、候选槽、三个代理调用和文献记录身份。

候选 ID 仍由规范内容生成，模型不能指定。候选转换后只形成待登记包；人工或正式编排器将全部 benchmark arm 映射到同一个现有 `ResearchFamilySpec`，再使用现有 CLI 登记候选和 campaign，随后才能在公司服务器计算。

## 16. 测试策略

### 16.1 纯合成逻辑测试

按照项目硬规则，Mac 允许且仅允许运行不接触公司真实数据、网络服务和 outcome 的程序生成合成逻辑测试；这些测试用于快速开发反馈，不构成正式研究验收。使用合成图谱验证：

- 完整图谱只能生成规定的脱敏字段；
- 真实因子 ID、公式、路径、IP、邮箱、密钥样式和禁词无法外发；
- 缺少或过期的公司外发政策不能被个人授权替代；
- 字段别名可确定性往返映射但映射不进入外发体；
- gap eligible filter、类别配额、priority tuple 和并列哈希得到确定性十张卡片；
- 同一可见身份的新 campaign 不能逃离原 discovery family；
- 四个 shadow arm 各 30 槽并映射到同一 120 槽统计 family；
- discovery context 与 candidate evaluation data identity 分离，重叠时只能产生探索性字段；
- 120 槽未全部终结时不能 seal，seal 后不能补抽或改槽，评价器不能绕过 seal；
- 研究失败、基础设施失败、API 消耗和统计 trial 分账；
- LLM 只能输出 `PredictionProposal`，`TestablePredictionSpec` 的冻结评价字段只能由固定程序注入；
- 字段 availability 从所有 AST 叶节点推导，非 point-in-time 字段被拒绝；
- 单位不相容、轴错误、shape 错误和非完整 rolling 被拒绝；
- 三个代理分别只能看到最小输入；
- 代理 2 和代理 3 请求没有 tools；
- 文献工具拒绝任意 URL、私网和敏感查询；
- 文献 identity、passage 和 claim support 三层状态不能互相冒充；
- 文献用途四分类、新颖性通道配额和反证检索局限可确定性核验；
- JSON、Schema、语义类型、availability、DSL、lookback、前视和重复检查失败关闭；
- 代理 3 不能改写假设或 AST；
- 请求拓扑最坏值等于 45，代理 3 按假设批量审查；
- 每次发送前预留 input/output，重试和 reasoning usage 计入预算；
- 授权哈希变化时失效；
- 对象级状态转换、文件锁、sequence、原子写入、fsync、幂等恢复和录制重放可发现篡改或 crash；
- API Key 不进入成功与失败输出。

同一套离线测试必须在公司 Linux 正式验收中重跑；Mac 通过不能替代 Linux 通过。

### 16.2 协议测试

使用注入的假 DeepSeek 客户端和假文献适配器：

- 不产生真实网络请求；
- 精确验证 `extra_body`、JSON 提示、请求参数、tool loop 和必要 reasoning 回传；
- 验证 429、5xx、`insufficient_system_resource`、空 content、截断、非法 JSON、格式修复和硬失败；
- 验证相同录制响应生成相同草案、审查和候选包。
- 验证进程在 request 保存前、in-flight、response 保存后和对象发布后的四类 crash 恢复。

### 16.3 公司 Linux 验收

使用当前代码提交和锁定依赖：

1. 运行完整测试；
2. 从冻结 V0.4 图谱生成真实脱敏 brief；
3. 核对 brief 不含真实因子 ID、路径、精确 IC 或完整矩阵；
4. 核验有效公司外发政策，输出外发预览并完成人工哈希授权；
5. 设置 `DEEPSEEK_API_KEY` 后运行一个有界真实 campaign；
6. 完成同一 discovery family 的 literature-only LLM、机械变异和 hypothesis-conditioned grammar shadow arm；
7. 核对三个代理输入、claim-level 文献记录、槽位、响应哈希和 token 预算；
8. 证明 120 槽全部进入终态后生成不可逆 seal，seal 前评价端口硬失败；
9. 把四个 arm 映射到同一个 120 槽现有统计 family；
10. 核验 discovery context 与 candidate evaluation 身份；若无法形成严格时间留出，只运行并标记探索性筛选；
11. 重复 `campaign-verify` 与 `family-verify` 得到相同内容身份；
12. 分别判断 operational acceptance 与独立 quality assessment。

没有 API Key 时只能验收离线 brief、预览和录制重放，不得声称在线 V0.5 已完成。

## 17. 验收标准

### 17.1 运行验收

运行协议验收通过必须同时满足：

- 真实完整图谱从未进入外部请求；
- 外发 payload 通过公司政策、白名单 Schema、敏感信息扫描和人工哈希授权；
- DeepSeek 只能使用官方端点和冻结模型；
- 三个代理输入、工具和输出职责相互隔离；
- 只有代理 1 可以使用受限文献检索；
- 来源的 identity、passage 和 claim support 状态没有混用；
- gap 选择、10×3、四个 shadow arm、120 槽 family、API 次数、文献查询和 token 上限不可绕过；
- 所有失败、拒绝、空槽和修复尝试进入追加式账本；
- 对象级状态机和 crash recovery 通过故障注入；
- `TestablePredictionSpec`、字段 availability 和 DSL 语义类型通过固定检查；
- 合法输出能转换为现有 `TrustedCandidateFactorSpec`；
- 120 槽未全部终结并完成不可逆 seal 时不得读取 outcome；
- discovery 与 evaluation 数据有重叠时只输出探索性筛选，不输出选择偏差已受控的统计结论；
- 真实生成结果只称为待验证候选；
- Mac 纯合成测试与公司 Linux 正式离线测试均通过；
- 在线调用只有在用户提供服务器环境密钥并确认外发预览后才执行。

30 个候选全部失败但所有状态可审计时，只能称为“运行协议验收完成”，不能称为生成质量通过。

### 17.2 生成质量验收

每个 LLM arm 独立计算：

```text
hypothesis_schema_valid_rate
mechanism_claim_supported_rate
proxy_choice_supported_rate
background_only_rate
novel_unverified_rate
candidate_schema_valid_rate
compiler_pass_rate
duplicate_rate
semantic_lint_pass_rate
privacy_violation_count
unauthorized_request_count
```

首版冻结最低质量：

- 假设 Schema 合法率至少 80%；
- `mechanism_claim_supported` 或 `proxy_choice_supported` 合计至少覆盖 60% 假设槽；
- `novel_unverified` 每 10 个假设槽最多 2 个；
- 候选 AST Schema 合法率至少 80%；
- 固定 compiler 通过率至少 60%；
- canonical 重复率不高于 30%；
- semantic lint 通过率至少 50%；
- 隐私违规和未授权外发必须为 0。

未达到任一阈值时，独立 `CampaignQualityAssessment` 标记为 `failed`，campaign 生命周期仍按实际 operational 状态记录。不得读取 candidate outcome 后在同一 discovery family 内调提示、换模型或补抽来修复质量指标。

### 17.3 Shadow benchmark

对比 A 只比较 `coverage_outcome_llm` 与 `literature_only_llm`，估计覆盖图谱结果上下文对假设和下游候选的增量价值。对比 B 只在相同已批准 coverage hypothesis 内比较 LLM 代理 2、机械变异和 hypothesis-conditioned grammar，估计表达式生成器的增量价值。

共同报告 Schema、compiler、重复、结构新颖性以及 seal 后候选评价；假设接受率和文献用途只对适用的 LLM 假设 arm 报告。每项指标同时标明固定预留槽分母、适用槽分母和未执行数，不把不适用计成失败，也不进行四臂总排名。

若使用严格时间留出，可报告 fresh visible RankIC、HAC、120 槽 Bonferroni、输出冗余和 V0.2 残差增量；若复用 discovery 数据，只报告 exploratory filter 指标并显式披露上游选择偏差未被 Bonferroni 控制。比较只报告效果量和不确定性，不因 benchmark 结果选择性删除 arm 或缩小 120 槽 family。

## 18. 采用依据

DeepSeek 官方文档确认：

- V4 正式模型名为 `deepseek-v4-pro` 与 `deepseek-v4-flash`；
- Chat Completions 支持 JSON Output 和 Tool Calls；
- thinking 模式支持 `high` 与 `max` 推理强度；
- thinking 模式不支持 temperature、top_p、presence penalty 和 frequency penalty；
- thinking 工具调用的后续轮次必须回传相应 `reasoning_content`。

实现时必须以锁定日期的官方文档和录制协议测试为准：

- `https://api-docs.deepseek.com/guides/json_mode/`
- `https://api-docs.deepseek.com/guides/thinking_mode`
- `https://api-docs.deepseek.com/guides/tool_calls`
- `https://api-docs.deepseek.com/api/create-chat-completion`
