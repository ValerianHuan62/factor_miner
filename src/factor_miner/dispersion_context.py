"""冻结股票总体的收盘收益分散程度；不读取标签或未来交易状态。"""
from pathlib import Path
import hashlib
import json

import polars as pl


CONTRACT = {
    'version': 'dispersion-context-v1', 'field': 'dispersion_change',
    'observation': 'close_t', 'availability': 'after_close_t',
    'definition': 'difference(sum(abs(stock_return-market_return))/(N-1))',
    'population': 'compute_t_and_compute_previous_market_day_and_rank_t',
    'price_basis': 'price_return_excluding_dividends',
    'min_names': 500, 'initial_null_sessions': 2,
}


def build_dispersion_context(market: pl.LazyFrame, state: pl.LazyFrame,
                             calendar: pl.DataFrame, index: pl.DataFrame) -> pl.DataFrame:
    """按精确市场日连接前收盘；首日无历史，后续不足500只则硬失败。"""
    days = calendar.select('date').sort('date')
    if days.height < 3 or days['date'].null_count() or days['date'].n_unique() != days.height:
        raise ValueError('分散程度日历缺失或重复')
    if index.schema != {'date': pl.Date, 'market_return': pl.Float64}:
        raise ValueError('分散程度市场收益结构不符')
    index = index.sort('date')
    if not index['date'].equals(days['date']) or index['market_return'].null_count() or not index['market_return'].is_finite().all():
        raise ValueError('分散程度市场收益必须完整覆盖日历')
    keys = ['date', 'asset']
    for source in (market, state):
        if source.select(keys).unique().select(pl.len()).collect().item() != source.select(pl.len()).collect().item():
            raise ValueError('分散程度输入主键重复')
        if source.filter(pl.any_horizontal(pl.col(k).is_null() for k in keys)).limit(1).collect().height:
            raise ValueError('分散程度输入主键缺失')
        if source.select('date').unique().join(days.lazy(), on='date', how='anti').limit(1).collect().height:
            raise ValueError('分散程度输入日期在日历之外')
    if any(state.collect_schema().get(k) != pl.Boolean for k in ['valid_for_factor_compute', 'valid_for_factor_rank']):
        raise ValueError('分散程度状态必须为显式布尔掩码')
    masks = state.select(*keys, 'valid_for_factor_compute', 'valid_for_factor_rank')
    if masks.filter(pl.any_horizontal(pl.col(k).is_null() for k in ['valid_for_factor_compute', 'valid_for_factor_rank'])).limit(1).collect().height:
        raise ValueError('分散程度状态缺失')
    if market.select(keys).join(masks.select(keys), on=keys, how='anti').limit(1).collect().height or masks.select(keys).join(market.select(keys), on=keys, how='anti').limit(1).collect().height:
        raise ValueError('分散程度价格和状态主键不一致')
    prices = market.select(*keys, 'close').join(masks, on=keys, validate='1:1')
    prior = prices.select(pl.col('date').alias('prior_date'), 'asset', pl.col('close').alias('prior_close'),
                          pl.col('valid_for_factor_compute').alias('prior_compute'))
    endpoints = days.with_columns(pl.col('date').shift(1).alias('prior_date'))
    eligible = prices.join(endpoints.lazy(), on='date', validate='m:1').join(prior, on=['prior_date', 'asset'], how='left', validate='m:1').filter(
        pl.col('valid_for_factor_compute') & pl.col('valid_for_factor_rank') & pl.col('prior_compute') &
        pl.col('close').is_finite() & (pl.col('close') > 0) & pl.col('prior_close').is_finite() & (pl.col('prior_close') > 0)
    ).with_columns((pl.col('close') / pl.col('prior_close') - 1).alias('stock_return'))
    stats = eligible.join(index.lazy(), on='date', validate='m:1').group_by('date').agg(
        pl.len().alias('names'), (pl.col('stock_return') - pl.col('market_return')).abs().sort_by('asset').sum().alias('absolute_sum')
    ).collect()
    result = days.join(stats, on='date', how='left', validate='1:1').sort('date')
    if result['names'][0] is not None or result['names'][1:].null_count() or (result['names'][1:] < 500).any():
        raise ValueError('首日应无前收盘；其后每日至少500只合格股票')
    result = result.with_columns((pl.col('absolute_sum') / (pl.col('names') - 1)).alias('dispersion')).with_columns(
        pl.col('dispersion').diff().alias('dispersion_change'))
    if not result['dispersion_change'][2:].is_finite().all():
        raise ValueError('分散程度差分出现非有限值')
    return result


def attach_dispersion_context(frame: pl.LazyFrame, calendar_path: Path,
                              context_path: Path, contract_path: Path) -> pl.LazyFrame:
    """绑定原始输入及人口统计快照，完整日期连接，严格限制初始空值。"""
    contract = json.loads(contract_path.read_text())
    if any(contract.get(k) != v for k, v in CONTRACT.items()):
        raise ValueError('分散程度合同或可得时点不支持')
    sources = contract.get('source_sha256', {})
    required = {'market_path', 'state_path', 'market_context_path', 'population_path'}
    if any(k not in contract or contract[k] not in sources for k in required):
        raise ValueError('分散程度来源身份未完整冻结')
    checks = [(context_path, contract.get('data_sha256')), (calendar_path, contract.get('calendar_sha256')),
              *((Path(p), digest) for p, digest in sources.items())]
    for path, expected in checks:
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                raise ValueError('分散程度冻结输入身份变化')
    if 'dispersion_change' in frame.collect_schema():
        raise ValueError('原始面板不得覆盖分散程度字段')
    days = pl.read_parquet(calendar_path).select('date').sort('date')
    context = pl.read_parquet(context_path).sort('date')
    if context.schema != {'date': pl.Date, 'dispersion_change': pl.Float64} or days.height < 3 or days['date'].null_count() or days['date'].n_unique() != days.height or not context['date'].equals(days['date']):
        raise ValueError('分散程度上下文必须精确覆盖唯一日历')
    values = context['dispersion_change']
    if values[:2].null_count() != 2 or values[2:].null_count() or not values[2:].is_finite().all():
        raise ValueError('分散程度仅允许最初两个交易日为结构性空值')
    population = pl.read_parquet(contract['population_path']).sort('date')
    if not population['date'].equals(days['date']) or population['names'][0] is not None or population['absolute_sum'][0] is not None or population['names'][1:].null_count() or (population['names'][1:] < 500).any() or population['absolute_sum'][1:].null_count() or not population['absolute_sum'][1:].is_finite().all() or (population['absolute_sum'][1:] < 0).any():
        raise ValueError('分散程度总体统计覆盖或样本量不符')
    expected = population.select('date', (pl.col('absolute_sum') / (pl.col('names') - 1)).diff().alias('dispersion_change'))
    if not expected.equals(context):
        raise ValueError('分散程度与冻结总体统计不一致')
    if frame.select('date').unique().join(days.lazy(), on='date', how='anti').limit(1).collect().height:
        raise ValueError('股票面板日期在分散程度日历之外')
    return frame.join(context.lazy(), on='date', how='left', validate='m:1')
