-- 修复阶段 B H01/H02/H04/H05 候选的 Dashboard 可读字段。
-- 原始 CandidateSpec 不可变；本迁移只重建 PostgreSQL 读模型，不改指标、哈希或运行身份。

BEGIN;

-- 阶段 A 的两个公开背景来源已核验；它们不替代候选的独立机制检验。
UPDATE candidates
SET source_refs = ARRAY[
    '来源：Campbell、Grossman、Wang（1993）《Trading Volume and Serial Correlation in Stock Returns》，The Quarterly Journal of Economics，108(4)，905–939，DOI: 10.2307/2118454；https://academic.oup.com/qje/article-abstract/108/4/905/1899978',
    '来源：Lee、Swaminathan（2000）《Price Momentum and Trading Volume》，The Journal of Finance，55，2017–2069，DOI: 10.1111/0022-1082.00280；https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.00280'
]
WHERE candidate_id IN ('huan001', 'huan002', 'huan003');

UPDATE candidates
SET
    factor_name_zh = '成交量强度—下跌收益反转交互因子（均量20日、跌幅5日）',
    formula_text = '((volume / MA_20(volume)) × (−(Δ_5(close))))',
    calculation_method = '计算当日成交量相对20日成交量均值的比值，再与5日收盘价变化的负向部分相乘；信号在下一交易日开盘使用。',
    factor_category = '成交量与短期反转',
    hypothesis_claim = '下跌后出现异常高成交量可能意味着卖压衰竭，并预示短期价格反转（预期收益为正）。',
    hypothesis_mechanism = '下跌期间的高成交量可能反映恐慌性卖出与知情交易者承接；卖压消退后，价格可能均值回归。',
    observable_proxy = '当日成交量相对20日均量的比值，与5日收盘价变化为负的条件交互。',
    independent_verification = '在不同市场和时间区间按成交量异常与下跌状态分组，控制波动率和流动性后检验后续收益。',
    competing_explanations = ARRAY['流动性提供者的库存风险补偿可能解释该效应，而非知情交易。'],
    failure_modes = ARRAY['趋势行情中可能出现错误反转信号。', '新闻导致的成交量放大可能不反转。', '效应可能随时间或市值分组衰减。'],
    falsification_path = '若独立区间中该模式不再成立，或扣除交易成本后消失，则否定该假设。',
    source_refs = ARRAY['来源：DeepSeek 生成（阶段 B，H01）；本批次未提供可核验外部来源，禁止作为研究引用。']
WHERE candidate_id = 'huan004';

UPDATE candidates
SET
    factor_name_zh = '成交量—下跌收益相关因子（20日、跌幅5日）',
    formula_text = 'Corr_20(volume, (−(Δ_5(close))))',
    calculation_method = '计算20日内成交量与5日收盘价变化负向部分的滚动相关系数；信号在下一交易日开盘使用。',
    factor_category = '成交量与短期反转',
    hypothesis_claim = '下跌后出现异常高成交量可能意味着卖压衰竭，并预示短期价格反转（预期收益为正）。',
    hypothesis_mechanism = '下跌期间的高成交量可能反映恐慌性卖出与知情交易者承接；卖压消退后，价格可能均值回归。',
    observable_proxy = '20日内成交量与5日收盘价变化负向部分的滚动相关系数。',
    independent_verification = '在不同市场和时间区间按成交量异常与下跌状态分组，控制波动率和流动性后检验后续收益。',
    competing_explanations = ARRAY['流动性提供者的库存风险补偿可能解释该效应，而非知情交易。'],
    failure_modes = ARRAY['趋势行情中可能出现错误反转信号。', '新闻导致的成交量放大可能不反转。', '效应可能随时间或市值分组衰减。'],
    falsification_path = '若独立区间中该模式不再成立，或扣除交易成本后消失，则否定该假设。',
    source_refs = ARRAY['来源：DeepSeek 生成（阶段 B，H01）；本批次未提供可核验外部来源，禁止作为研究引用。']
WHERE candidate_id = 'huan005';

UPDATE candidates
SET
    factor_name_zh = '成交量强度—下跌收益反转交互因子（均量10日、跌幅3日）',
    formula_text = '((volume / MA_10(volume)) × (−(Δ_3(close))))',
    calculation_method = '计算当日成交量相对10日成交量均值的比值，再与3日收盘价变化的负向部分相乘；信号在下一交易日开盘使用。',
    factor_category = '成交量与短期反转',
    hypothesis_claim = '下跌后出现异常高成交量可能意味着卖压衰竭，并预示短期价格反转（预期收益为正）。',
    hypothesis_mechanism = '下跌期间的高成交量可能反映恐慌性卖出与知情交易者承接；卖压消退后，价格可能均值回归。',
    observable_proxy = '当日成交量相对10日均量的比值，与3日收盘价变化为负的条件交互。',
    independent_verification = '在不同市场和时间区间按成交量异常与下跌状态分组，控制波动率和流动性后检验后续收益。',
    competing_explanations = ARRAY['流动性提供者的库存风险补偿可能解释该效应，而非知情交易。'],
    failure_modes = ARRAY['趋势行情中可能出现错误反转信号。', '新闻导致的成交量放大可能不反转。', '效应可能随时间或市值分组衰减。'],
    falsification_path = '若独立区间中该模式不再成立，或扣除交易成本后消失，则否定该假设。',
    source_refs = ARRAY['来源：DeepSeek 生成（阶段 B，H01）；本批次未提供可核验外部来源，禁止作为研究引用。']
WHERE candidate_id = 'huan006';

UPDATE candidates
SET
    factor_name_zh = '成交量强度—收益自相关差异因子（20日）',
    formula_text = '((volume / MA_20(volume)) − Corr_20(Δ_1(close), Delay_1(Δ_1(close))))',
    calculation_method = '计算成交量相对20日均量的比值，减去20日内1日收益与其1日滞后收益的滚动相关系数；信号在下一交易日开盘使用。',
    factor_category = '成交量与收益自相关',
    hypothesis_claim = '相对近期均值的成交量冲击可能造成价格短期过度调整，并在随后一周修正。',
    hypothesis_mechanism = '流动性需求冲击可能使价格偏离基本价值；流动性恢复后价格回归。',
    observable_proxy = '成交量相对20日均量的比值，与收益及其滞后收益的20日滚动相关差异。',
    independent_verification = '在多个市场和时间区间检验大成交量冲击后的反转，并控制买卖价差反弹、市场冲击、波动率与流动性。',
    competing_explanations = ARRAY['该效应可能来自做市商库存管理，而非错误定价。', '也可能来自大单造成的暂时价格压力。'],
    failure_modes = ARRAY['高度流动市场中冲击可能不足以造成过度反应。', '危机期持续订单失衡可能使反转失败。'],
    falsification_path = '若控制买卖价差反弹和市场冲击后，异常成交量不能预测后续收益反转，则否定该假设。',
    source_refs = ARRAY['来源：DeepSeek 生成（阶段 B，H02）；本批次未提供可核验外部来源，禁止作为研究引用。']
WHERE candidate_id = 'huan007';

UPDATE candidates
SET
    factor_name_zh = '成交量强度—收益联动差异因子（60日）',
    formula_text = '((volume / MA_60(volume)) − Corr_60(volume, Δ_1(close)))',
    calculation_method = '计算成交量相对60日均量的比值，减去60日内成交量与1日收盘价变化的滚动相关系数；信号在下一交易日开盘使用。',
    factor_category = '成交量与价格联动',
    hypothesis_claim = '相对近期均值的成交量冲击可能造成价格短期过度调整，并在随后一周修正。',
    hypothesis_mechanism = '流动性需求冲击可能使价格偏离基本价值；流动性恢复后价格回归。',
    observable_proxy = '成交量相对60日均量的比值，与60日内成交量和1日收盘价变化滚动相关的差异。',
    independent_verification = '在多个市场和时间区间检验大成交量冲击后的反转，并控制买卖价差反弹、市场冲击、波动率与流动性。',
    competing_explanations = ARRAY['该效应可能来自做市商库存管理，而非错误定价。', '也可能来自大单造成的暂时价格压力。'],
    failure_modes = ARRAY['高度流动市场中冲击可能不足以造成过度反应。', '危机期持续订单失衡可能使反转失败。'],
    falsification_path = '若控制买卖价差反弹和市场冲击后，异常成交量不能预测后续收益反转，则否定该假设。',
    source_refs = ARRAY['来源：DeepSeek 生成（阶段 B，H02）；本批次未提供可核验外部来源，禁止作为研究引用。']
WHERE candidate_id = 'huan008';

UPDATE candidates
SET
    factor_name_zh = '成交量变化—收益相关差异因子（120日）',
    formula_text = '((volume / MA_120(volume)) − Corr_120(Δ_1(volume), Δ_1(close)))',
    calculation_method = '计算成交量相对120日均量的比值，减去120日内1日成交量变化与1日收盘价变化的滚动相关系数；信号在下一交易日开盘使用。',
    factor_category = '成交量与价格联动',
    hypothesis_claim = '相对近期均值的成交量冲击可能造成价格短期过度调整，并在随后一周修正。',
    hypothesis_mechanism = '流动性需求冲击可能使价格偏离基本价值；流动性恢复后价格回归。',
    observable_proxy = '成交量相对120日均量的比值，与120日内1日成交量变化和1日收盘价变化滚动相关的差异。',
    independent_verification = '在多个市场和时间区间检验大成交量冲击后的反转，并控制买卖价差反弹、市场冲击、波动率与流动性。',
    competing_explanations = ARRAY['该效应可能来自做市商库存管理，而非错误定价。', '也可能来自大单造成的暂时价格压力。'],
    failure_modes = ARRAY['高度流动市场中冲击可能不足以造成过度反应。', '危机期持续订单失衡可能使反转失败。'],
    falsification_path = '若控制买卖价差反弹和市场冲击后，异常成交量不能预测后续收益反转，则否定该假设。',
    source_refs = ARRAY['来源：DeepSeek 生成（阶段 B，H02）；本批次未提供可核验外部来源，禁止作为研究引用。']
WHERE candidate_id = 'huan009';

UPDATE candidates
SET
    factor_name_zh = '成交量强度—收益自相关差异因子（5日）',
    formula_text = '((volume / MA_5(volume)) − Corr_5(Δ_1(close), Delay_1(Δ_1(close))))',
    calculation_method = '计算成交量相对5日均量的比值，减去5日内1日收益与其1日滞后收益的滚动相关系数；信号在下一交易日开盘使用。',
    factor_category = '成交量与收益自相关',
    hypothesis_claim = '近期成交量上升可能强化价格趋势，预示短期趋势延续。',
    hypothesis_mechanism = '成交量上升可能反映投资者关注与机构累积，从而维持价格动量。',
    observable_proxy = '成交量相对5日均量与5日收益自相关的差异。',
    independent_verification = '在不同时间区间和波动率分组中，比较该表达式与价格动量基线的后续收益和风险调整表现。',
    competing_explanations = ARRAY['成交量可能只是波动率的代理变量。', '该关系也可能由羊群交易共同驱动。'],
    failure_modes = ARRAY['熊市中的高成交量上涨可能无法延续。', '趋势末端的成交量激增可能预示反转。'],
    falsification_path = '若加入成交量后的表达式在风险调整后不能优于价格动量基线，则否定该假设。',
    source_refs = ARRAY['来源：DeepSeek 生成（阶段 B，H04）；本批次未提供可核验外部来源，禁止作为研究引用。']
WHERE candidate_id = 'huan010';

UPDATE candidates
SET
    factor_name_zh = '成交量强度—收益联动差异因子（10日）',
    formula_text = '((volume / MA_10(volume)) − Corr_10(volume, Δ_1(close)))',
    calculation_method = '计算成交量相对10日均量的比值，减去10日内成交量与1日收盘价变化的滚动相关系数；信号在下一交易日开盘使用。',
    factor_category = '成交量与价格联动',
    hypothesis_claim = '近期成交量上升可能强化价格趋势，预示短期趋势延续。',
    hypothesis_mechanism = '成交量上升可能反映投资者关注与机构累积，从而维持价格动量。',
    observable_proxy = '成交量相对10日均量与10日内成交量—1日收盘价变化滚动相关的差异。',
    independent_verification = '在不同时间区间和波动率分组中，比较该表达式与价格动量基线的后续收益和风险调整表现。',
    competing_explanations = ARRAY['成交量可能只是波动率的代理变量。', '该关系也可能由羊群交易共同驱动。'],
    failure_modes = ARRAY['熊市中的高成交量上涨可能无法延续。', '趋势末端的成交量激增可能预示反转。'],
    falsification_path = '若加入成交量后的表达式在风险调整后不能优于价格动量基线，则否定该假设。',
    source_refs = ARRAY['来源：DeepSeek 生成（阶段 B，H04）；本批次未提供可核验外部来源，禁止作为研究引用。']
WHERE candidate_id = 'huan011';

UPDATE candidates
SET
    factor_name_zh = '成交量强度—收益自相关差异因子（20日）',
    formula_text = '((volume / MA_20(volume)) − Corr_20(Δ_1(close), Delay_1(Δ_1(close))))',
    calculation_method = '计算成交量相对20日均量的比值，减去20日内1日收益与其1日滞后收益的滚动相关系数；信号在下一交易日开盘使用。',
    factor_category = '成交量与收益自相关',
    hypothesis_claim = '近期成交量上升可能强化价格趋势，预示短期趋势延续。',
    hypothesis_mechanism = '成交量上升可能反映投资者关注与机构累积，从而维持价格动量。',
    observable_proxy = '成交量相对20日均量与20日收益自相关的差异。',
    independent_verification = '在不同时间区间和波动率分组中，比较该表达式与价格动量基线的后续收益和风险调整表现。',
    competing_explanations = ARRAY['成交量可能只是波动率的代理变量。', '该关系也可能由羊群交易共同驱动。'],
    failure_modes = ARRAY['熊市中的高成交量上涨可能无法延续。', '趋势末端的成交量激增可能预示反转。'],
    falsification_path = '若加入成交量后的表达式在风险调整后不能优于价格动量基线，则否定该假设。',
    source_refs = ARRAY['来源：DeepSeek 生成（阶段 B，H04）；本批次未提供可核验外部来源，禁止作为研究引用。']
WHERE candidate_id = 'huan012';

UPDATE candidates
SET
    factor_name_zh = '成交量强度×短期收益交互因子（均量120日）',
    formula_text = '((volume / MA_120(volume)) × Δ_1(close))',
    calculation_method = '计算成交量相对120日成交量均值的比值，再与1日收盘价变化相乘；信号在下一交易日开盘使用。',
    factor_category = '成交量与短期动量',
    hypothesis_claim = '相对长期均值极高的成交量可能削弱收益自相关，降低收益持续性。',
    hypothesis_mechanism = '高成交量可能反映信息流增强，加快价格发现并降低过去收益对未来收益的预测性。',
    observable_proxy = '成交量相对120日均量与1日收盘价变化的交互项。',
    independent_verification = '在不同市场和时间区间用预测回归检验成交量强度与滞后收益的交互项，并进行样本外检验。',
    competing_explanations = ARRAY['高成交量可能来自噪声交易而非信息。', '该关系也可能是市场微观结构效应。'],
    failure_modes = ARRAY['高度流动市场中成交量可能不影响收益自相关。', '投机泡沫中高成交量可能与强动量同时出现。'],
    falsification_path = '若样本外交互项不能稳定呈预期负向，或方向只由少数区间驱动，则否定该假设。',
    source_refs = ARRAY['来源：DeepSeek 生成（阶段 B，H05）；本批次未提供可核验外部来源，禁止作为研究引用。']
WHERE candidate_id = 'huan013';

UPDATE candidates
SET
    factor_name_zh = '成交量强度×短期收益交互因子（均量60日）',
    formula_text = '((volume / MA_60(volume)) × Δ_1(close))',
    calculation_method = '计算成交量相对60日成交量均值的比值，再与1日收盘价变化相乘；信号在下一交易日开盘使用。',
    factor_category = '成交量与短期动量',
    hypothesis_claim = '相对长期均值极高的成交量可能削弱收益自相关，降低收益持续性。',
    hypothesis_mechanism = '高成交量可能反映信息流增强，加快价格发现并降低过去收益对未来收益的预测性。',
    observable_proxy = '成交量相对60日均量与1日收盘价变化的交互项。',
    independent_verification = '在不同市场和时间区间用预测回归检验成交量强度与滞后收益的交互项，并进行样本外检验。',
    competing_explanations = ARRAY['高成交量可能来自噪声交易而非信息。', '该关系也可能是市场微观结构效应。'],
    failure_modes = ARRAY['高度流动市场中成交量可能不影响收益自相关。', '投机泡沫中高成交量可能与强动量同时出现。'],
    falsification_path = '若样本外交互项不能稳定呈预期负向，或方向只由少数区间驱动，则否定该假设。',
    source_refs = ARRAY['来源：DeepSeek 生成（阶段 B，H05）；本批次未提供可核验外部来源，禁止作为研究引用。']
WHERE candidate_id = 'huan014';

UPDATE candidates
SET
    factor_name_zh = '成交量强度×短期收益交互因子（均量20日）',
    formula_text = '((volume / MA_20(volume)) × Δ_1(close))',
    calculation_method = '计算成交量相对20日成交量均值的比值，再与1日收盘价变化相乘；信号在下一交易日开盘使用。',
    factor_category = '成交量与短期动量',
    hypothesis_claim = '相对长期均值极高的成交量可能削弱收益自相关，降低收益持续性。',
    hypothesis_mechanism = '高成交量可能反映信息流增强，加快价格发现并降低过去收益对未来收益的预测性。',
    observable_proxy = '成交量相对20日均量与1日收盘价变化的交互项。',
    independent_verification = '在不同市场和时间区间用预测回归检验成交量强度与滞后收益的交互项，并进行样本外检验。',
    competing_explanations = ARRAY['高成交量可能来自噪声交易而非信息。', '该关系也可能是市场微观结构效应。'],
    failure_modes = ARRAY['高度流动市场中成交量可能不影响收益自相关。', '投机泡沫中高成交量可能与强动量同时出现。'],
    falsification_path = '若样本外交互项不能稳定呈预期负向，或方向只由少数区间驱动，则否定该假设。',
    source_refs = ARRAY['来源：DeepSeek 生成（阶段 B，H05）；本批次未提供可核验外部来源，禁止作为研究引用。']
WHERE candidate_id = 'huan015';

COMMIT;
