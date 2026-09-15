"""独立的单因子状态假设小实验；不连接默认筛选、组合或数据库。"""
from datetime import date, datetime, timezone
from pathlib import Path
import json
import shutil
import time

import numpy as np
import polars as pl
from scipy.stats import norm

from factor_miner.ridge_strategy import file_hash


def write_json(path: Path, value: object) -> None:
    """严格写入新文件，非有限数不能伪装成可用结果。"""
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False, default=str)


def pressure_state(daily: pl.DataFrame, calendar: pl.DataFrame, *, smooth: int = 20,
                   history: int = 252, min_coverage: float = .8) -> pl.DataFrame:
    """按完整市场日历平滑价差，信号日只用前一交易日及更早报价。"""
    if calendar['date'].null_count() or calendar['date'].n_unique() != calendar.height:
        raise ValueError('市场日历为空值或重复')
    if daily['date'].n_unique() != daily.height:
        raise ValueError('每日市场价差重复')
    frame = calendar.sort('date').join(daily, on='date', how='left', validate='1:1')
    frame = frame.with_columns(pl.when(
        (pl.col('coverage') >= min_coverage) & pl.col('median_spread').is_finite()
        & (pl.col('median_spread') >= 0)
    ).then(pl.col('median_spread')).alias('_valid_spread'))
    frame = frame.with_columns(
        pl.col('_valid_spread').rolling_mean(smooth, min_samples=smooth).shift(1).alias('pressure'))
    frame = frame.with_columns(
        pl.col('pressure').shift(1).rolling_median(history, min_samples=history).alias('threshold'))
    return frame.with_columns(pl.when(pl.col('pressure').is_not_null() & pl.col('threshold').is_not_null())
        .then((pl.col('pressure') > pl.col('threshold')).cast(pl.Int8)).alias('high_pressure'))


def state_contrasts(values: list, states: list, *, lags: int, family: int,
                    min_per_state: int = 26) -> dict:
    """两组均值和差异的 Bartlett HAC；缺失周保留在原时间网格。"""
    y = np.array([np.nan if v is None else v for v in values], dtype=float)
    h = np.array([np.nan if v is None else v for v in states], dtype=float)
    valid = np.isfinite(y) & np.isfinite(h)
    if np.any(valid & ~np.isin(h, [0, 1])):
        raise ValueError('状态只能为高压或低压')
    low_n, high_n = int(np.sum(valid & (h == 0))), int(np.sum(valid & (h == 1)))
    if min(low_n, high_n) < min_per_state:
        return dict(status='insufficient_state_dates', low_dates=low_n, high_dates=high_n)
    x = np.column_stack((1-h[valid], h[valid]))
    bread = np.linalg.inv(x.T @ x)
    coef = bread @ x.T @ y[valid]
    # 缺失周的估计方程贡献为零，不能把相隔多周的样本压成相邻周。
    score = np.zeros((len(y), 2))
    score[valid] = x * (y[valid] - x @ coef)[:, None]
    meat = score.T @ score
    for lag in range(1, min(lags, len(y)-1)+1):
        cross = score[lag:].T @ score[:-lag]
        meat += (1-lag/(lags+1)) * (cross + cross.T)
    covariance = bread @ meat @ bread
    result = dict(status='evaluated', low_dates=low_n, high_dates=high_n, missing_dates=int((~valid).sum()))
    contrasts = {'low': [1, 0], 'high': [0, 1], 'difference': [-1, 1],
                 'unconditional': [low_n/(low_n+high_n), high_n/(low_n+high_n)]}
    for name, weights in contrasts.items():
        c = np.array(weights)
        estimate = float(c @ coef)
        variance = float(c @ covariance @ c)
        if not np.isfinite(variance) or variance <= 0:
            result[name] = dict(estimate=estimate, status='degenerate_variance')
            continue
        se = float(np.sqrt(variance))
        p = float(2*norm.sf(abs(estimate/se)))
        result[name] = dict(estimate=estimate, standard_error=se, hac_t=estimate/se,
            raw_p=p, bonferroni_p=min(1., p*family), ci95=[estimate-1.95996398454*se, estimate+1.95996398454*se])
    return result


def weekly_diagnostics(panel: pl.DataFrame, states: pl.DataFrame, calendar: pl.DataFrame,
                       factor: str, sign: int, start: date, boundary: date, end: date) -> pl.DataFrame:
    """复用原共同股票池；分组先于标签过滤，固定 T+1/T+6 和事件边界。"""
    panel = panel.filter(pl.col('date').is_between(start, end))
    if panel.select(pl.struct('date', 'asset').is_duplicated().any()).item():
        raise ValueError('候选面板主键重复')
    if panel.filter(~pl.col(factor).is_finite() | pl.col(factor).is_null()).height:
        raise ValueError('原共同股票池因子缺失')
    calendar = calendar.sort('date').with_columns(pl.col('date').shift(-6).alias('_exit'))
    panel = panel.join(calendar, on='date', how='left', validate='m:1')
    if panel.filter(pl.col('_exit').is_null()).height:
        raise ValueError('日历不足以容纳固定退出日')
    finite = pl.col('label_o2o_5d').is_finite().fill_null(False)
    if panel.filter(finite & (pl.col('label_exit_date').is_null()
                             | (pl.col('label_exit_date') != pl.col('_exit')))).height:
        raise ValueError('标签退出日违反固定 T+6 合同')
    panel = panel.with_columns((sign*pl.col(factor)).alias('score'))
    panel = panel.with_columns((pl.col('score').rank('average').over('date')/pl.len().over('date')).alias('percentile'))
    # 开发与复核边界按标签退出事件 purge；输出仍保存该周及原证券计数。
    usable = finite & ~((pl.col('date') < boundary) & (pl.col('_exit') >= boundary))
    panel = panel.with_columns(pl.when(usable).then(pl.col('label_o2o_5d')).alias('target'))
    base = panel.group_by('date').agg(pl.len().alias('securities'), pl.col('target').count().alias('labeled'),
        (pl.col('percentile') > .9).sum().alias('top_selected'), (pl.col('percentile') <= .1).sum().alias('bottom_selected'))
    observed = panel.filter(pl.col('target').is_not_null()).group_by('date').agg(
        pl.corr('score', 'target', method='spearman').alias('rank_ic'),
        pl.col('target').filter(pl.col('percentile') > .9).mean().alias('top_gross_mean'),
        pl.col('target').filter(pl.col('percentile') <= .1).mean().alias('bottom_gross_mean'),
        pl.col('target').filter(pl.col('percentile') > .9).count().alias('top_labeled'),
        pl.col('target').filter(pl.col('percentile') <= .1).count().alias('bottom_labeled'))
    weekly = base.join(observed, on='date', how='left', validate='1:1').join(
        states.select('date', 'pressure', 'threshold', 'high_pressure'), on='date', how='left', validate='1:1')
    weekly = weekly.with_columns((pl.col('labeled')/pl.col('securities')).alias('label_coverage'))
    return weekly.with_columns(
        pl.when((pl.col('labeled') >= 20) & (pl.col('label_coverage') >= .8) & pl.col('rank_ic').is_finite())
        .then(pl.col('rank_ic')).alias('rank_ic'),
        (pl.col('top_gross_mean')-pl.col('bottom_gross_mean')).alias('gross_spread')
    ).sort('date')


def run_state_hypothesis_pilot(config_path: Path) -> Path:
    """执行已冻结的一次比较；先验证输入，后计算状态，最后才读取收益。"""
    config = json.loads(config_path.read_text())
    if config['version'] != 'single-state-pilot-v1' or config['factor_id'] != 'huan007' or config['direction'] != 1:
        raise ValueError('此小实验只支持已冻结 huan007 原方向')
    if config['diagnostic_family_size'] != config['inherited_family_size'] + 8:
        raise ValueError('必须预留两个分区各四项统计名额')
    if not config['test_consumed'] or config['sealed_oos']:
        raise ValueError('历史使用状态不能改变')
    root = Path(config['output_root'])
    root.mkdir(parents=True, exist_ok=False)
    write_json(root/'protocol.json', config)
    write_json(root/'registration.json', dict(registered_at=datetime.now(timezone.utc), status='registered'))
    shutil.copy2(__file__, root/'state_hypothesis_pilot.py')
    try:
        for path, expected in config['input_sha256'].items():
            if file_hash(Path(path)) != expected:
                raise ValueError(f'冻结输入身份改变：{path}')
        write_json(root/'code_identity.json', dict(module_sha256=file_hash(Path(__file__))))
        dataset = Path(config['dataset_root'])
        manifest = json.loads((dataset/'manifest.json').read_text())
        if manifest['availability'] != 'after_close_t' or manifest['quote_price_basis'] != 'unadjusted_same_day_usd':
            raise ValueError('报价可得时间或价格单位不明')
        for name in ('market.parquet', 'state.parquet', 'calendar.parquet'):
            if config['input_sha256'][str(dataset/name)] != manifest['files'][name]['sha256']:
                raise ValueError('冻结输入与报价发布不一致')
        source = json.loads(Path(config['factor_report_path']).read_text())
        if source['hypothesis']['expected_sign'] != 'positive' or source['candidate_id'] != config['candidate_id']:
            raise ValueError('候选身份或原方向不匹配')
        calendar = pl.read_parquet(dataset/'calendar.parquet')
        market = pl.scan_parquet(dataset/'market.parquet').select('date', 'asset', 'quote_bid_raw', 'quote_ask_raw', 'valid_closing_quote')
        eligibility = pl.scan_parquet(dataset/'state.parquet').select('date', 'asset', 'valid_for_factor_rank')
        quotes = market.join(eligibility, on=['date', 'asset'], validate='1:1').filter(pl.col('valid_for_factor_rank'))
        quotes = quotes.with_columns(pl.when(pl.col('valid_closing_quote')
            & pl.col('quote_bid_raw').is_finite() & pl.col('quote_ask_raw').is_finite()
            & (pl.col('quote_bid_raw') > 0) & (pl.col('quote_ask_raw') >= pl.col('quote_bid_raw')))
            .then(2*(pl.col('quote_ask_raw')-pl.col('quote_bid_raw'))/(pl.col('quote_ask_raw')+pl.col('quote_bid_raw'))).alias('spread'))
        daily = quotes.group_by('date').agg(pl.len().alias('eligible'), pl.col('spread').count().alias('quoted'),
            pl.col('spread').median().alias('median_spread')).with_columns((pl.col('quoted')/pl.col('eligible')).alias('coverage')).collect()
        states = pressure_state(daily, calendar)
        states.write_parquet(root/'daily_state.parquet')
        # 只读取信号日期作状态可行性检查，不读取目标收益。
        dates = pl.scan_parquet(config['panel_path']).select('date').unique().filter(
            pl.col('date').is_between(date.fromisoformat(config['start']), date.fromisoformat(config['end']))).collect()
        counts = dates.join(states, on='date', how='left').group_by(pl.col('date').dt.year().alias('year')).agg(
            pl.len().alias('signal_dates'), pl.col('high_pressure').count().alias('state_dates'),
            (pl.col('high_pressure') == 1).sum().alias('high_dates'), (pl.col('high_pressure') == 0).sum().alias('low_dates')).sort('year')
        write_json(root/'preflight.json', dict(label_values_read=False, annual_state_coverage=counts.to_dicts()))
        if counts.filter(pl.col('year') >= 2024).select(pl.col('state_dates').sum()).item() < 60:
            raise ValueError('历史复核可用状态少于60周，不读取收益')
        panel = pl.read_parquet(config['panel_path'], columns=['date', 'asset', config['factor_id'], 'label_o2o_5d', 'label_exit_date'])
        start, boundary, end = [date.fromisoformat(config[k]) for k in ('start', 'boundary', 'end')]
        weekly = weekly_diagnostics(panel, states, calendar, config['factor_id'], config['direction'], start, boundary, end)
        weekly.write_parquet(root/'weekly_diagnostics.parquet')
        annual = weekly.filter(pl.col('high_pressure').is_not_null()).with_columns(
            pl.col('date').dt.year().alias('year'), pl.when(pl.col('high_pressure') == 1).then(pl.lit('高压力')).otherwise(pl.lit('低压力')).alias('state'))
        annual = annual.group_by('year', 'state').agg(pl.col('rank_ic').count().alias('valid_weeks'), pl.col('rank_ic').mean().alias('rank_ic'),
            pl.col('gross_spread').mean().alias('gross_spread'), pl.col('label_coverage').mean().alias('label_coverage')).sort('year', 'state')
        write_json(root/'annual.json', annual.to_dicts())
        summary = dict(factor_id=config['factor_id'], test_consumed=True, sealed_oos=False, mechanism_status='mechanism_unverified',
            diagnostic_family_size=config['diagnostic_family_size'], stages={})
        for stage, data in [('development', weekly.filter(pl.col('date') < boundary)), ('historical_review', weekly.filter(pl.col('date') >= boundary))]:
            summary['stages'][stage] = state_contrasts(data['rank_ic'].to_list(), data['high_pressure'].to_list(), lags=5,
                family=config['diagnostic_family_size'])
        review = summary['stages']['historical_review']
        if review['status'] != 'evaluated':
            summary['screening_decision'] = '状态样本不足，不能判断'
        elif review['difference']['estimate'] <= 0:
            summary['screening_decision'] = '历史复核未支持高压力增强假设；不改原因子结论'
        elif review['high']['estimate'] <= 0:
            summary['screening_decision'] = '高压力下仍无正向平均RankIC；不支持条件保留'
        elif max(review['difference'].get('bonferroni_p', 1), review['high'].get('bonferroni_p', 1)) >= .05:
            summary['screening_decision'] = '点估计符合状态假设，但证据不足以通过条件筛选'
        else:
            summary['screening_decision'] = '通过本次历史条件筛选；仍需独立未消费数据确认'
        write_json(root/'summary.json', summary)
        lines = ['# 单因子状态筛选小实验', '', summary['screening_decision'], '',
            '研究对象：美股 huan007（1日反转），保持原正方向。新增状态假设为市场报价价差压力较高时反转作用更强。', '',
            '市场压力为每日可排名股票相对收盘买卖价差的截面中位数，再取20个完整交易日均值。信号只使用前一交易日及更早报价；超过此前252个压力观察的中位数为高压，其余为低压。', '',
            '复用原28因子完整值共同股票池及周度信号；标签为固定T+1至T+6开盘收益。开发期2021—2023，历史复核2024—2025；开发期按退出事件清除跨界标签。历史已消费，不称为密封样本外。', '',
            '| 分区 | 全状态平均RankIC | 高压力 | 低压力 | 高减低 | 差异HAC t | 差异原始p | 差异校正p |',
            '|---|---:|---:|---:|---:|---:|---:|---:|']
        for name, r in summary['stages'].items():
            if r['status'] == 'evaluated':
                d = r['difference']
                lines.append(f"| {name} | {r['unconditional']['estimate']:.5f} | {r['high']['estimate']:.5f} | {r['low']['estimate']:.5f} | {d['estimate']:.5f} | {d.get('hac_t',float('nan')):.3f} | {d.get('raw_p',float('nan')):.5f} | {d.get('bonferroni_p',float('nan')):.5f} |")
            else:
                lines.append(f"| {name} | 样本不足 | — | — | — | — | — | — |")
        lines += ['', '| 年份 | 状态 | 有效周 | 平均RankIC | 五日顶部减底部毛收益 | 标签覆盖 |', '|---|---|---:|---:|---:|---:|']
        for r in annual.to_dicts():
            if r['rank_ic'] is not None:
                lines.append(f"| {r['year']} | {r['state']} | {r['valid_weeks']} | {r['rank_ic']:.5f} | {r['gross_spread']:.3%} | {r['label_coverage']:.2%} |")
        lines += ['', '## 判断边界', '',
            '高低压力均值差直接使用完整周网格上的5阶Bartlett HAC，缺失周不压缩。两分区各四项统计共预留8项，继承既有预算；校正只作保守历史诊断，不能消除过去适应性选择。年度表仅作描述，不用于选择阈值或年份。', '',
            '顶部减底部为按信号预先分组的可观测五日标签毛收益，尚未处理交易成本、借券、未成交和终止持仓，不是可交易组合收益。条件有效性与净收益需要分别判断。', '',
            '本实验只评估一个既有因子的状态假设，不能证明整个筛选方法优于其他方法，也不验证论文BTQ资金流机制。市场中位价差仍可能受股票组成、价格水平等影响；报价为历史修订发布，无逐日存档版本保证。', '',
            '原始候选、方向、账本、Dashboard及默认筛选规则不变；状态假设、输入身份、覆盖率、逐周结果和年度结果全部保留在本独立目录。']
        (root/'报告.md').write_text('\n'.join(lines)+'\n')
        for path, expected in config['input_sha256'].items():
            if file_hash(Path(path)) != expected:
                raise ValueError(f'运行期间输入改变：{path}')
        write_json(root/'completion.json', dict(status='completed', completed_at=datetime.now(timezone.utc),
            result_sha256={p.name:file_hash(p) for p in root.iterdir() if p.is_file()}, inputs_unchanged=True))
    except Exception as exc:
        write_json(root/'failure.json', dict(status='failed', error=str(exc), failed_at=datetime.now(timezone.utc)))
        raise
    return root


def market_state_levels(market: pl.LazyFrame, eligibility: pl.LazyFrame, calendar: pl.DataFrame) -> pl.DataFrame:
    """只读行情生成市场波动、路径持续性和相对交易活跃，不读取标签。"""
    indexed = calendar.sort('date').with_row_index('_session')
    data = market.select('date', 'asset', 'close', 'volume').join(
        eligibility.select('date', 'asset', 'valid_for_factor_compute', 'valid_for_factor_rank'),
        on=['date', 'asset'], validate='1:1').join(indexed.lazy(), on='date', validate='m:1').sort('asset', 'date')
    usable = pl.col('valid_for_factor_compute') & pl.col('close').is_finite() & (pl.col('close') > 0)
    data = data.with_columns(pl.when(usable).then(pl.col('close')).alias('_price'),
        pl.when(usable & pl.col('volume').is_finite() & (pl.col('volume') >= 0))
        .then(pl.col('close')*pl.col('volume')).alias('_dollar'))
    data = data.with_columns(pl.when(pl.col('_session')-pl.col('_session').shift(1).over('asset') == 1)
        .then(pl.col('_price')/pl.col('_price').shift(1).over('asset')-1).alias('_ret'),
        pl.when(pl.col('_session')-pl.col('_session').shift(59).over('asset') == 59)
        .then(pl.col('_dollar').rolling_mean(5, min_samples=5).over('asset')
              /pl.col('_dollar').rolling_mean(60, min_samples=60).over('asset')).alias('_activity'))
    data = data.with_columns(pl.when(pl.col('_activity').is_finite()).then(pl.col('_activity')).alias('_activity'))
    daily = data.filter(pl.col('valid_for_factor_rank')).group_by('date').agg(
        pl.col('_ret').mean().alias('market_return'), pl.col('_ret').is_not_null().mean().alias('return_coverage'),
        pl.col('_activity').median().alias('activity'), pl.col('_activity').is_not_null().mean().alias('activity_coverage')).collect()
    daily = calendar.sort('date').join(daily, on='date', how='left', validate='1:1')
    daily = daily.with_columns(pl.when(pl.col('return_coverage') >= .8).then(pl.col('market_return')).alias('_return'),
        pl.when(pl.col('activity_coverage') >= .8).then(pl.col('activity')).alias('activity'))
    return daily.with_columns(pl.col('_return').rolling_std(20, min_samples=20).alias('volatility'),
        (pl.col('_return').rolling_sum(20, min_samples=20).abs()
         /pl.col('_return').abs().rolling_sum(20, min_samples=20)).alias('trend'))


def score_ensembles(panel: pl.DataFrame, wide_states: pl.DataFrame, factors: list[dict]) -> pl.DataFrame:
    """原方向等权与按事前状态等权；无可用状态不生成条件预测。"""
    ids = [f['factor_id'] for f in factors]
    if any(panel[k].null_count() or not panel[k].is_finite().all() for k in ids):
        raise ValueError('共同股票池原分数不完整')
    panel = panel.join(wide_states, on='date', how='left', validate='m:1')
    conditions = [pl.col(f['state_id']) == f['active_value'] for f in factors]
    known = pl.all_horizontal([pl.col(f['state_id']).is_not_null() for f in factors])
    active = pl.sum_horizontal([c.cast(pl.Int64) for c in conditions])
    scores = [f['direction']*pl.col(f['factor_id']) for f in factors]
    numerator = pl.sum_horizontal([pl.when(c).then(s).otherwise(0.) for c, s in zip(conditions, scores)])
    return panel.with_columns((pl.sum_horizontal(scores)/len(factors)).alias('_always'),
        pl.when(known).then(active).alias('active_factors'), known.alias('states_known'),
        pl.when(known & (active > 0)).then(numerator/active).alias('_conditional'))


def align_combination_weeks(always: pl.DataFrame, conditional: pl.DataFrame, scored: pl.DataFrame) -> pl.DataFrame:
    """连接后显式恢复完整周顺序，避免哈希连接打乱HAC滞后。"""
    frame = always.select('date',pl.col('rank_ic').alias('always_ic')).join(
        conditional.select('date',pl.col('rank_ic').alias('conditional_ic')),on='date',how='left',validate='1:1').join(
        scored.group_by('date').agg(pl.col('active_factors').first(),pl.col('states_known').first()),on='date',validate='1:1')
    return frame.with_columns((pl.col('conditional_ic')-pl.col('always_ic')).alias('paired_difference')).sort('date')


def influence_inference(estimate: float, influence: np.ndarray, family: int) -> dict:
    """对按日期对齐的联合影响函数计算HAC，保留跨因子同期相关。"""
    variance = float(influence @ influence)
    for lag in range(1, min(5, len(influence)-1)+1):
        variance += 2*(1-lag/6)*float(influence[lag:] @ influence[:-lag])
    if variance <= 0 or not np.isfinite(variance):
        return dict(estimate=estimate, status='degenerate_variance')
    se = float(np.sqrt(variance))
    p = float(2*norm.sf(abs(estimate/se)))
    return dict(estimate=estimate, standard_error=se, hac_t=estimate/se, raw_p=p,
                bonferroni_p=min(1., p*family), ci95=[estimate-1.95996398454*se, estimate+1.95996398454*se])


def mean_inference(values: list, family: int) -> dict:
    """完整周网格上的均值推断，不将停用或缺失周当成零IC。"""
    y = np.array([np.nan if x is None else x for x in values], dtype=float)
    valid = np.isfinite(y)
    if valid.sum() < 26:
        return dict(status='insufficient_dates', valid_weeks=int(valid.sum()))
    estimate = float(y[valid].mean())
    influence = np.zeros(len(y)); influence[valid] = (y[valid]-estimate)/valid.sum()
    return dict(valid_weeks=int(valid.sum()), **influence_inference(estimate, influence, family))


def joint_state_uplift(series: list[dict], family: int) -> dict:
    """先对机制家族等权，再对家族内因子等权，避免重复公式主导总证据。"""
    groups = sorted({s['mechanism_family'] for s in series})
    estimate, total = 0., np.zeros(len(series[0]['values']))
    for item in series:
        y = np.array([np.nan if x is None else x for x in item['values']], dtype=float)
        h = np.array([np.nan if x is None else x for x in item['states']], dtype=float)
        valid = np.isfinite(y) & np.isfinite(h)
        high, low = valid & (h == 1), valid & (h == 0)
        if min(high.sum(), low.sum()) < 26:
            return dict(status='insufficient_state_dates', factor_id=item['factor_id'])
        mu_high, mu_low = float(y[high].mean()), float(y[low].mean())
        weight = 1/len(groups)/sum(s['mechanism_family'] == item['mechanism_family'] for s in series)
        estimate += weight*(mu_high-mu_low)
        influence = np.zeros(len(y))
        influence[high] = (y[high]-mu_high)/high.sum()
        influence[low] = -(y[low]-mu_low)/low.sum()
        total += weight*influence
    return dict(factors=len(series), mechanism_families=len(groups), **influence_inference(estimate, total, family))


def run_state_hypothesis_batch(config_path: Path) -> Path:
    """运行有限多因子比较及一个固定条件组合对照，不修改候选筛选身份。"""
    config = json.loads(config_path.read_text())
    factors = config['factors']; count = len(factors)
    capacity = count*8+10
    if config['version'] != 'state-hypothesis-batch-v1' or count < 10 or len({f['factor_id'] for f in factors}) != count:
        raise ValueError('需要至少10个不同的事前冻结因子')
    if config['diagnostic_capacity'] != capacity or config['diagnostic_family_size'] != config['inherited_family_size']+capacity:
        raise ValueError('完整比较预算不一致')
    if not config['test_consumed'] or config['sealed_oos']:
        raise ValueError('不得重置历史已消费状态')
    root = Path(config['output_root']); root.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    write_json(root/'protocol.json', config)
    write_json(root/'registration.json', dict(registered_at=datetime.now(timezone.utc), label_values_read=False))
    shutil.copy2(__file__, root/'state_hypothesis_pilot.py')
    def checkpoint():
        elapsed = time.monotonic()-started
        size = sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
        if elapsed >= config['resource_limits']['wall_seconds'] or size >= config['resource_limits']['artifact_bytes']:
            raise ValueError('本轮资源上限已达到')
        return dict(wall_seconds=elapsed, artifact_bytes=size)
    try:
        for path, expected in config['input_sha256'].items():
            if file_hash(Path(path)) != expected:
                raise ValueError(f'输入身份改变：{path}')
        for f in factors:
            report = json.loads(Path(f['report_path']).read_text())
            if report['candidate_id'] != f['candidate_id'] or {'positive':1,'negative':-1}[report['hypothesis']['expected_sign']] != f['direction']:
                raise ValueError('原因子身份或方向不一致')
            if f['active_value'] not in (0, 1) or not f['expected_state'] or not f['failure_condition']:
                raise ValueError('状态假设和失效条件未完整登记')
        write_json(root/'hypothesis_cards.json', factors)
        dataset = Path(config['dataset_root'])
        manifest = json.loads((dataset/'manifest.json').read_text())
        for name in ('market.parquet','state.parquet','calendar.parquet'):
            if manifest['files'][name]['sha256'] != config['input_sha256'][str(dataset/name)]:
                raise ValueError('数据发布与冻结哈希不一致')
        calendar = pl.read_parquet(dataset/'calendar.parquet')
        levels = market_state_levels(pl.scan_parquet(dataset/'market.parquet'), pl.scan_parquet(dataset/'state.parquet'), calendar)
        states = {'liquidity':pl.read_parquet(config['liquidity_state_path'])}
        for name in ('volatility','trend','activity'):
            daily = levels.select('date', pl.col(name).alias('median_spread'),
                                  pl.when(pl.col(name).is_finite()).then(1.).otherwise(0.).alias('coverage'))
            states[name] = pressure_state(daily, calendar, smooth=1)
        pl.concat([frame.select('date','pressure','threshold','high_pressure').with_columns(pl.lit(name).alias('state_id'))
                   for name, frame in states.items()]).write_parquet(root/'daily_states.parquet')
        dates = pl.scan_parquet(config['panel_path']).select('date').unique().filter(
            pl.col('date').is_between(date.fromisoformat(config['start']), date.fromisoformat(config['end']))).collect().sort('date')
        wide_states = calendar.select('date')
        coverage = []
        for name, state in states.items():
            wide_states = wide_states.join(state.select('date',pl.col('high_pressure').alias(name)),on='date',validate='1:1')
            coverage.extend(dates.join(state,on='date',how='left').group_by(pl.col('date').dt.year().alias('year')).agg(
                pl.len().alias('weeks'),pl.col('high_pressure').count().alias('known_weeks'),
                (pl.col('high_pressure')==1).sum().alias('high_weeks'),(pl.col('high_pressure')==0).sum().alias('low_weeks'))
                .with_columns(pl.lit(name).alias('state_id')).to_dicts())
        write_json(root/'preflight.json',dict(label_values_read=False,state_coverage=coverage))
        checkpoint()
        panel = pl.read_parquet(config['panel_path'],columns=['date','asset','label_o2o_5d','label_exit_date']+[f['factor_id'] for f in factors])
        start, boundary, end = [date.fromisoformat(config[k]) for k in ('start','boundary','end')]
        family = config['diagnostic_family_size']
        diagnostics, results, annual = {}, [], []
        for f in factors:
            state = states[f['state_id']]
            if f['active_value'] == 0:
                state = state.with_columns((1-pl.col('high_pressure')).alias('high_pressure'))
            weekly = weekly_diagnostics(panel, state, calendar, f['factor_id'], f['direction'],start,boundary,end)
            diagnostics[f['factor_id']] = weekly
            result = dict(factor_id=f['factor_id'],name=f['name'],mechanism_family=f['mechanism_family'],stages={})
            for stage, data in [('development',weekly.filter(pl.col('date') < boundary)),('historical_review',weekly.filter(pl.col('date') >= boundary))]:
                result['stages'][stage] = state_contrasts(data['rank_ic'].to_list(),data['high_pressure'].to_list(),lags=5,family=family)
            for row in weekly.filter(pl.col('high_pressure').is_not_null()).group_by(pl.col('date').dt.year().alias('year'),'high_pressure').agg(
                pl.col('rank_ic').count().alias('valid_weeks'),pl.col('rank_ic').mean().alias('rank_ic'),
                pl.col('gross_spread').mean().alias('gross_spread')).sort('year','high_pressure').to_dicts():
                annual.append(dict(factor_id=f['factor_id'],**row))
            r = result['stages']['historical_review']
            if r['status'] != 'evaluated':
                result['decision'] = '状态样本不足'
            elif r['high']['estimate'] <= 0:
                result['decision'] = '预期适用状态下平均IC仍不为正'
            elif r['difference']['estimate'] <= 0:
                result['decision'] = '状态增强方向不符合'
            elif max(r['high'].get('bonferroni_p',1),r['difference'].get('bonferroni_p',1)) < .05:
                result['decision'] = '通过当前历史条件诊断'
            else:
                result['decision'] = '方向符合，统计证据不足'
            r_low = r.get('low',{})
            result['weak_state_assessment'] = ('预期弱状态出现负平均IC' if r_low.get('estimate',0)<0 else '弱状态仍有正平均IC，不能称已失效')
            results.append(result)
            checkpoint()
        pl.concat([frame.with_columns(pl.lit(fid).alias('factor_id')) for fid,frame in diagnostics.items()]).write_parquet(root/'factor_weekly.parquet')
        write_json(root/'factor_results.json',results); write_json(root/'annual.json',annual)
        # 组合只用事前状态和原方向；当周无激活因子时不生成预测，不伪造零IC。
        scored = score_ensembles(panel,wide_states,factors)
        base_state = calendar.with_columns(pl.lit(0.).alias('pressure'),pl.lit(0.).alias('threshold'),pl.lit(0).alias('high_pressure'))
        always = weekly_diagnostics(scored,base_state,calendar,'_always',1,start,boundary,end)
        conditional = weekly_diagnostics(scored.filter(pl.col('_conditional').is_not_null()),base_state,calendar,'_conditional',1,start,boundary,end)
        combo = align_combination_weeks(always,conditional,scored)
        combo.write_parquet(root/'combination_weekly.parquet')
        aggregate, model_results = {}, {}
        for stage in ('development','historical_review'):
            predicate = pl.col('date') < boundary if stage == 'development' else pl.col('date') >= boundary
            data = combo.filter(predicate)
            paired = data.with_columns(pl.when(pl.col('paired_difference').is_finite()).then(pl.col('always_ic')).alias('paired_always'))
            model_results[stage] = dict(always_on_paired_dates=mean_inference(paired['paired_always'].to_list(),family),
                conditional_on_paired_dates=mean_inference(paired['conditional_ic'].to_list(),family),
                paired_difference=mean_inference(paired['paired_difference'].to_list(),family),
                total_weeks=data.height,no_active_weeks=data.filter(pl.col('active_factors')==0).height,
                unknown_state_weeks=data.filter(~pl.col('states_known')).height)
            series = []
            for f in factors:
                w = diagnostics[f['factor_id']].filter(predicate)
                series.append(dict(factor_id=f['factor_id'],mechanism_family=f['mechanism_family'],values=w['rank_ic'].to_list(),states=w['high_pressure'].to_list()))
            aggregate[stage] = dict(all_factors=joint_state_uplift(series,family),
                excluding_previously_seen=joint_state_uplift([s for s in series if s['factor_id'] != 'huan007'],family))
        write_json(root/'aggregate.json',aggregate); write_json(root/'combination_results.json',model_results)
        summary = dict(factor_count=count,mechanism_families=len({f['mechanism_family'] for f in factors}),
            positive_state_difference=sum(r['stages']['historical_review'].get('difference',{}).get('estimate',0)>0 for r in results),
            positive_active_ic=sum(r['stages']['historical_review'].get('high',{}).get('estimate',0)>0 for r in results),
            raw_significant_positive_difference=sum(r['stages']['historical_review'].get('difference',{}).get('estimate',0)>0
                and r['stages']['historical_review'].get('difference',{}).get('raw_p',1)<.05 for r in results),
            historical_condition_pass=sum(r['decision']=='通过当前历史条件诊断' for r in results),
            primary_comparison=model_results['historical_review'],aggregate=aggregate['historical_review'],
            test_consumed=True,sealed_oos=False,diagnostic_family_size=family)
        write_json(root/'summary.json',summary)
        render_batch_report(root,config,results,annual,model_results,aggregate)
        for path,expected in config['input_sha256'].items():
            if file_hash(Path(path))!=expected: raise ValueError(f'运行期间输入变化：{path}')
        usage = checkpoint()
        write_json(root/'completion.json',dict(status='completed',completed_at=datetime.now(timezone.utc),usage=usage,
            inputs_unchanged=True,result_sha256={p.name:file_hash(p) for p in root.iterdir() if p.is_file()}))
    except Exception as exc:
        write_json(root/'failure.json',dict(error=str(exc),failed_at=datetime.now(timezone.utc)))
        raise
    return root


def render_batch_report(root: Path, config: dict, results: list, annual: list, models: dict, aggregate: dict) -> None:
    """中文报告展示全部因子和事前失效条件，不隐藏反例。"""
    lines = ['# 十二因子状态假设与条件使用比较','',
        '原方向、状态和失效条件在本轮结果前登记。复用原28因子完整值共同股票池，周度信号、固定T+1至T+6五日开盘标签。开发期2021—2023，历史复核2024—2025，最晚信号2025-12-12。历史区间已被使用，不能称密封样本外。','',
        '## 事前适用状态与失效条件','',
        '| 因子 | 名称 | 预期适用状态 | 预期弱化或失效条件 |','|---|---|---|---|']
    for f in config['factors']:
        lines.append(f"| {f['factor_id']} | {f['name']} | {f['expected_state']} | {f['failure_condition']} |")
    lines += ['', '失效条件是待检验命题。某状态均值较低不等于因子已失效；没有投资者行为或独立机制数据时仍保持机制未独立验证。', '',
        '## 历史复核逐因子比较','',
        '| 因子 | 全状态IC | 适用状态IC | 其余状态IC | 适用减其余 | 差异原始p | 差异校正p | 2024差异 | 2025差异 | 判断 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for item in results:
        r = item['stages']['historical_review']
        if r['status'] != 'evaluated':
            lines.append(f"| {item['factor_id']} | — | — | — | — | — | — | — | — | 状态样本不足 |")
            continue
        deltas=[]
        for year in (2024,2025):
            rows={x['high_pressure']:x['rank_ic'] for x in annual if x['factor_id']==item['factor_id'] and x['year']==year}
            deltas.append(rows[1]-rows[0] if rows.get(1) is not None and rows.get(0) is not None else float('nan'))
        d=r['difference']
        lines.append(f"| {item['factor_id']} | {r['unconditional']['estimate']:.4f} | {r['high']['estimate']:.4f} | {r['low']['estimate']:.4f} | {d['estimate']:+.4f} | {d.get('raw_p',float('nan')):.4f} | {d.get('bonferroni_p',float('nan')):.4f} | {deltas[0]:+.4f} | {deltas[1]:+.4f} | {item['decision']} |")
    lines += ['', '## 条件使用与始终使用','',
        '基准：12个原方向秩分数等权。条件方案：仅给处于各自预期状态的因子等权。无激活因子时不生成选股预测，不把未定义RankIC填为零。比较使用相同证券及双方有预测的配对周，并单列不参与的周数；这不是资金账户或净收益回测。','',
        '| 分区 | 配对周 | 始终使用IC | 条件使用IC | 配对改善 | 改善原始p | 95%区间 | 无激活周 | 未知状态周 |','|---|---:|---:|---:|---:|---:|---|---:|---:|']
    for stage,label in [('development','开发期'),('historical_review','历史复核')]:
        m=models[stage];d=m['paired_difference']
        if 'estimate' in d:
            ci=d.get('ci95',[float('nan')]*2)
            lines.append(f"| {label} | {d['valid_weeks']} | {m['always_on_paired_dates']['estimate']:.4f} | {m['conditional_on_paired_dates']['estimate']:.4f} | {d['estimate']:+.4f} | {d.get('raw_p',float('nan')):.4f} | [{ci[0]:.4f}, {ci[1]:.4f}] | {m['no_active_weeks']} | {m['unknown_state_weeks']} |")
    lines += ['', '## 跨因子相关与历史使用','',
        '因子分为四个机制家族。整体状态差异先在家族内等权，再对家族等权，HAC使用同一日期上的联合影响函数，保留因子间同期相关和跨期依赖；不把12个因子视为12次独立成功。huan007已在上一轮查看，额外报告剔除它后的11因子统计。','',
        '| 历史复核范围 | 家族等权状态差异 | 原始p | 95%区间 |','|---|---:|---:|---|']
    for key,label in [('all_factors','全部12个'),('excluding_previously_seen','剔除huan007的11个')]:
        r=aggregate['historical_review'][key]
        if 'estimate' in r:
            ci=r.get('ci95',[float('nan')]*2)
            lines.append(f"| {label} | {r['estimate']:+.4f} | {r.get('raw_p',float('nan')):.4f} | [{ci[0]:.4f}, {ci[1]:.4f}] |")
    lines += ['', '## 测量与结论边界','',
        '流动性状态复用上一轮20日市场中位报价价差压力。风险状态为当时可排名股票等权市场收益的20日波动；趋势状态为20日市场收益和的绝对值除以绝对收益和；交易活跃状态为个股5日相对60日美元成交额的截面中位数。全部滞后一个市场日，与此前252个状态观察中位数比较，不优化阈值。它们是可观察代理，不直接证明资金流、投资者偏好或信息传播。','',
        f"全部{config['diagnostic_capacity']}项预留统计继承历史后使用{config['diagnostic_family_size']}倍Bonferroni。校正仅作保守历史诊断；低统计功效不能被解释为经济效应不存在。年度方向和正向数量仅作描述。",'',
        '弱状态负平均IC只记录失效迹象，不自动确认失效；适用状态仍非正或差异反向时保留反例，不改原方向。条件使用最终是否值得采用，还需要成本、换手和独立新数据验证。本轮没有修改默认筛选或发布研究库。']
    (root/'报告.md').write_text('\n'.join(lines)+'\n')
