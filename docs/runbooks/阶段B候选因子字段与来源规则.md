# 阶段 B 候选因子字段与来源规则

## 研究边界

阶段 B 产生的是通过冻结协议和可见回测的候选因子，不是认证因子、有效 Alpha 或因果机制证明。经济机制在独立机制检验完成前必须标记为 `mechanism_unverified`。失败的假设或无效的模型响应必须保留失败记录，不得为了凑足候选数量而伪造因子。

## 候选目录的字段来源

`candidates` 的业务字段必须逐候选读取不可变的 `CandidateSpec` 和其中的 `hypothesis`，不得复制 `huan001–huan003`、上一批候选或固定模板：

- `factor_name_zh`：根据当前候选真实 AST 的运算含义、字段和窗口，用中文简要命名；不同公式不能共用不相符的名称。
- `formula_text`：由当前候选真实 AST 确定性渲染，必须保留实际字段、算子、周期和窗口；不能把 `neg`、`delta` 等节点省略。
- `calculation_method`：用中文说明当前公式的输入、运算、周期和最早交易时点；不得把未出现在 AST 中的 `traded_value`、换手率或其他字段写进去。
- `factor_category`：根据当前公式和假设的实际机制归类；不能因所用字段都为 `close`、`volume` 就统一归为同一类别。
- `required_fields`：只能来自 AST 实际出现的字段，并与服务器字段注册表一致。
- `hypothesis_claim`、`expected_sign`、`hypothesis_mechanism`、`observable_proxy`、`independent_verification`、`competing_explanations`、`failure_modes`、`falsification_path`：逐候选保留结果揭晓前冻结的假设内容；不能用别的候选覆盖。

审计字段如哈希、来源候选编号和运行编号可以放在后列，但不能替代前面的业务字段。

## 中文与来源规则

以下自由文本字段必须使用中文书写，字段名、公式算子、数据别名、`IC`、`Sharpe`、`Barra`、URL、DOI 等专有名词可以保留原文：

`hypothesis_mechanism`、`observable_proxy`、`independent_verification`、`competing_explanations`、`failure_modes`、`falsification_path`。

`source_refs` 必须说明来源性质，不能使用无法阅读的 `public_source_pending_review` 等占位符：

- 有可核验文献时，记录作者、年份、标题、期刊或工作论文编号、DOI 或稳定 URL；
- 只有 DeepSeek 生成、没有经过人工核验的外部文献时，明确记录“DeepSeek 生成；本批次未提供可核验外部来源，禁止作为研究引用”，不得伪造文献；
- 来源引用必须与假设实际支持的主张相匹配，不能把相邻主题的文献冒充直接证据。

## 投影与修改规则

原始候选 Spec、响应和账本是不可变研究产物。修正 Dashboard 可读字段时，只能重建或追加读模型投影，不得原地篡改原始 Spec、回测指标、哈希或运行身份。投影前必须检查：候选 ID 唯一、公式来自当前 AST、名称和类别不是上一候选模板、自由文本为中文、来源引用不是占位符、失败候选没有空的指标行。

以后每次阶段 B 生成、批准、表达式校验、服务器计算和 PostgreSQL 投影，都必须执行本规则，并在运行记录中保留验证结果。
