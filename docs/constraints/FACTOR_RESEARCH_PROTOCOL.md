# 因子研究协议

## 1. 层级

```text
Data Contract
  → Raw Factor Compute
  → Evaluation Preprocess
  → Visible RankIC / Statistics
  → Redundancy Gate
  → Incremental Information Gate
  → Visible Candidate Package
```

原始因子层不得混入 evaluation preprocess。candidate expression 只能生成 raw factor。

## 2. DSL

候选表达式只允许字段引用、有限常数和以下白名单算子：

```text
add sub mul div neg abs sign gt
delay delta calendar_delay calendar_delta
rolling_sum rolling_mean rolling_std rolling_skew rolling_min rolling_max rolling_argmax rolling_time_corr rolling_cov rolling_corr rolling_partial_corr rolling_partial_beta
```

允许窗口为 `{5, 10, 20, 40, 60, 120}`；最大节点数 15、最大深度 5、最大 lookback 130。禁止标签字段、负 shift、centered rolling、动态字段、未知算子和任意 Python。

除零结果定义为 null/NaN，不添加隐式 epsilon；缺失值保持缺失，不 `fillna(0)`。

`rolling_argmax(x, window)` 返回完整有限观察窗口中最后一次最大值的位置，最早为0、最新为window-1，相同最大值选择最近一次。任一非有限值或窗口不足返回null，常数窗口返回window-1。输出无量纲，表示观察行位置而非自然日或市场交易日距离，不能称为截面排名或直接假定是52周高点。复用锁定的NumPy窗口视图与Polars分证券批处理，禁止按元素拆分窗口；参见[NumPy窗口接口](https://numpy.org/doc/stable/reference/generated/numpy.lib.stride_tricks.sliding_window_view.html)与[Polars批处理接口](https://docs.pola.rs/api/python/stable/reference/expressions/api/polars.Expr.map_batches.html)。

`rolling_time_corr(x, window)` 是单序列与窗口内从旧到新位置0至window-1的Pearson相关，无量纲。位置代表观察行，不是自然日。完整有限且非恒定窗口才计算，缺失、无穷、常量或非有限结果返回null，不添加epsilon或截断相关系数。正仿射变换保持结果，反转窗口顺序改变符号。它与“距今多久”的相关方向相反，也不是横截面排名；输入为收益时不能把历史收益顺序相关当作价格趋势。复用现有NumPy居中点积和Polars按证券非元素批处理，候选无权注入代码。

`rolling_cov(x, y, window)` 使用共同完整观察窗口计算样本协方差，固定ddof=1，单位为两输入单位乘积。任一非有限输入使该窗口为空，常数序列协方差为合法零值。复用已锁定Polars实现，不改变既有算子；完整边界见[滚动样本协方差合同](../contracts/滚动样本协方差.md)。

`rolling_partial_corr(x, y, z, window)` 测量移除同一窗口内控制序列 z 的截距和线性分量后，x、y 残差的 Pearson 相关。三个输入必须在相同的整个观察行窗口内全部有限；任何缺失都使窗口无效，不能使用各自不同的有效配对。采用 `(r_xy-r_xz*r_yz)/sqrt((1-r_xz²)*(1-r_yz²))`；常量控制、任一残差方差占原方差比例不超过 `1e-12` 或非有限结果返回 null。绝对值大于 `1+1e-10` 的结果同样无效，只将该误差以内的边界舍入到 ±1。这是显式数值可识别性限制，不给分母加 epsilon。输出无量纲，按证券分组、完整窗口和历史信息计算；偏相关只能移除线性共动，不能证明因果。

`rolling_partial_beta(x, y, z, window)` 返回在回归 `x = 截距 + b*y + c*z + 残差` 中的 b，单位为x单位除以y单位。三个输入共享全部有限的完整观察窗口；用共同样本协方差计算 `(cov_xy-cov_xz*cov_yz/var_z)/(var_y-cov_yz²/var_z)`。z或y方差非正、y去除z后残差方差比例不超过1e-12或结果非有限则null，不加epsilon。x为常量时斜率可以为零，不把零敏感度当作缺失；斜率不限制在±1，也不等同于偏相关。窗口与未来信息限制继续相同。 可选第四输入w形成双控制回归，所有四个序列共用完整窗口；控制相关矩阵行列式须大于1e-12，y在两个控制上投影后的残差比例也须大于1e-12，否则null。旧三参数计算次序不变。

`rolling_skew(x, window)` 固定为调整后的Fisher-Pearson样本偏度G1，即sqrt(n*(n-1))/(n-2)乘以m3/m2^(3/2)，中心矩mk为完整窗口内(x-均值)^k的均值。使用现有固定版本Polars的bias=False、min_samples=window和center=False，所有输入须有限；常量窗口和非有限输出为null，不加epsilon、不截断偏度。无量纲、对正仿射变换不变，对符号反转变号。它是单个序列分布的形状统计，不是截面标准分，也不能被称为预期特质偏度或新机制的独立证明。参见[Polars偏度接口](https://docs.pola.rs/api/python/stable/reference/expressions/api/polars.Expr.rolling_skew.html)，升级运行版本须重新核验其固定语义。

`sign` 对有限正数、负数和零分别输出 1、−1、0；缺失、NaN 与正负无穷输出 null，不能把无效输入转换为有效方向。它是原始观测的确定性变换，不做截面标准化。包含该算子的新候选必须在读取结果前登记并绑定支持它的代码快照；既有运行继续沿用原代码身份。

`gt(x,y)` 要求两个输入单位相容，双方有限时返回严格大于事件的无量纲1或0，相等为0。任一缺失、NaN或无穷时输出null，不把未知事件当作未发生。该点态事件原语不做截面排名；滚动事件频率必须使用完整有效窗口，不因缺失而缩小分母。禁止附加window、period等无关参数，沿用原节点和深度限制。

`delay`、`delta` 和 rolling 按各证券的观察行计算，缺行时不能称为固定市场交易日跨度。`calendar_delay(field, period)` 按显式发布日历精确回看指定交易日，只允许原始字段作为参数；指定端点缺失就返回 null，不顺延到其他报价。该算子禁止负周期和嵌套计算，继续受相同复杂度与 lookback 上限约束。 `calendar_delta(field, period)` 是同一日历端点上的当前字段减过去字段，保留字段单位；其唯一参数同样只能是原始字段，不能隐藏任意公式。它与显式sub(field,calendar_delay(field,period))逐值相同，缺少端点返回null，仍须绑定显式市场日历。

固定日历算子需要由 `attach_market_sessions` 将发布的完整日历映射为保留列 `__market_session`。日历重复、主键重复、日期缺失、日历外行情或已有同名保留列必须失败；不增加证券行情行、不填价格。`run-research-report` 对包含该算子的候选显式附加日历上下文，并在编译计划记录 `explicit_market_session_index_v1`；其他调用方也必须提供校验后的上下文，缺失时不能退回观察行延迟。新语义使用新 AST 和候选身份，不覆盖旧方向、旧试验和旧代码快照。

## 3. 编译与计算

compiler 必须确定性地产生 Polars expression plan，并记录 canonical AST hash、required fields、lookback 和 plan hash。相同 spec、代码、配置和数据 release 必须产生相同 raw factor hash。

计算按 asset/date 排序，仅使用当前及历史数据。signal availability 与最早可交易时间必须在 profile 中声明；candidate 不得自行改变。

## 4. Evaluation

evaluation 从冻结的 profile 读取 mask、label、split 和预处理规则。公司 A 股 profile 使用 `valid_for_factor_rank` 计算截面 RankIC。原始 factor 先保留，评价阶段才执行冻结的截面处理。

固定持有期标签必须按统一市场交易日历生成，并保存 `label_entry_date` 和 `label_exit_date`。某证券在固定事件日缺行时保持 null，禁止按证券局部行号前移。训练/发现分区仅允许 `label_exit_date < next_split_start`，边界重叠样本必须 purge。

每个日期至少满足 campaign 的 `min_names_per_date`；整体必须满足 `min_valid_dates` 和 `min_median_coverage`。不足时返回明确 failure code，不能改变区间或股票池重试同一 trial。

## 5. 冗余

第一阶段检查 canonical AST hash、字段/窗口签名和显式结构重复；第二阶段计算 candidate 与 campaign/reference pool 的逐日截面 Spearman，并汇总总体与分年度结果。超过冻结阈值时标记 `REDUNDANCY_THRESHOLD_EXCEEDED`，但保留全部 artifact 与事件。

## 6. 增量信息

逐对冗余之后执行联合线性冗余检查。程序对每天的候选和冻结参考基底做平均秩、样本标准化和带截距 OLS，只将无法被参考空间解释的残差交给结果评价。

参考库内容 ID、有序因子集合、清单 SHA-256、覆盖率、最低超额股票数、条件数、最低残差方差比例和最低残差 RankIC 均在 outcome 前冻结。同批次更早候选按预登记顺序进入联合基底。禁止使用 PCA、正则化、自适应选因子或结果后删列。

残差使用与原始因子相同的 RankIC、HAC 和完整研究族 Bonferroni 口径。残差通过不代表非线性独立、因果机制、可交易收益或样本外有效。

## 7. 因果回测

回测顺序固定为：T 日因子和 `valid_for_factor_rank` 冻结名单与目标权重，T+1 开盘读取 `can_open_long` 并成交，计划退出日及以后逐日读取 `can_close_long`。任何未来成交状态、未来价格是否存在、退出是否成功都不能参与 T 日排名。

入场未成交权重留作现金，不替补、不追单。退出未成交持仓继续按可见价格估值并锁定资金，后续逐交易日重试；成本只对实际成交计提。数据终点存在未合法退出持仓时整个回测失败，不得删除证券、回退旧价格或补零。

## 8. 状态

合法终态只有：

```text
visible_passed
visible_failed
compile_failed
compute_failed
evaluation_failed
redundancy_failed
incremental_failed
interrupted
```

所有失败必须有稳定错误码和非零 CLI 退出码；禁止静默切换数据、标签、窗口、mask 或统计方法。
