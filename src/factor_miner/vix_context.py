"""显式冻结的Cboe VIX点数变化，最早在当晚收齐指数收盘后使用。"""
from datetime import date
from pathlib import Path
import hashlib
import json
import polars as pl


def attach_vix_context(frame: pl.LazyFrame, calendar_path: Path, context_path: Path, contract_path: Path) -> pl.LazyFrame:
    """核验CSV来源、精确日历差分、完整覆盖与收盘后可得合同，不补价格。"""
    return _attach_volatility_context(frame, calendar_path, context_path, contract_path, "vix")


def attach_vvix_context(frame: pl.LazyFrame, calendar_path: Path, context_path: Path, contract_path: Path) -> pl.LazyFrame:
    """VVIX使用独立字段与来源合同，等待所需指数全部收盘及供应商完整交付。"""
    return _attach_volatility_context(frame, calendar_path, context_path, contract_path, "vvix")


def attach_ovx_context(frame: pl.LazyFrame, calendar_path: Path, context_path: Path, contract_path: Path) -> pl.LazyFrame:
    """OVX采用独立来源合同；显式登记的源缺口传递为空，不补值或顺延。"""
    return _attach_volatility_context(frame, calendar_path, context_path, contract_path, "ovx")


def _attach_volatility_context(frame, calendar_path, context_path, contract_path, index):
    contract = json.loads(contract_path.read_text())
    field = f"{index}_change"
    expected = {'version':'vix-context-v1', 'field':'vix_change', 'observation':'index_close_t',
        'availability':'after_16_15_et_and_complete_vendor_delivery', 'earliest_trade':'open_t_plus_1',
        'definition':'vix_close_t-minus-vix_close_previous_stock_market_session',
        'unit':'volatility_index_points', 'revision_policy':'historical_download_no_vintage_guarantee'}
    if index in {'vvix', 'ovx'}:
        expected.update(version=f'{index}-context-v1', field=field,
            availability='after_required_index_sessions_finalized_and_complete_vendor_delivery',
            definition=f'{index}_close_t-minus-{index}_close_previous_stock_market_session')
    if index == 'ovx':
        expected['missing_policy'] = 'preregistered_source_gaps_propagate_null_without_fill'
    if any(contract.get(key) != value for key,value in expected.items()):
        raise ValueError(f'{index.upper()}上下文定义或可得时点不明确')
    source_path = Path(contract['source_snapshot_path'])
    for path,key in [(context_path,'data_sha256'),(calendar_path,'calendar_sha256'),(source_path,'source_snapshot_sha256')]:
        if hashlib.sha256(path.read_bytes()).hexdigest() != contract.get(key):
            raise ValueError(f'{index.upper()}上下文、来源或日历身份变化')
    if field in frame.collect_schema():
        raise ValueError(f'原面板不能覆盖显式{index.upper()}变化字段')
    context=pl.read_parquet(context_path)
    if context.schema != {'date':pl.Date,field:pl.Float64}:
        raise ValueError(f'{index.upper()}上下文字段必须为date与{field}')
    calendar=pl.read_parquet(calendar_path).select('date').sort('date')
    for dates in [context.select('date'),calendar]:
        if dates.is_empty() or dates['date'].null_count() or dates['date'].n_unique()!=dates.height:
            raise ValueError(f'{index.upper()}上下文或日历日期缺失、重复')
    if (index != 'ovx' and context[field].null_count()) or not context[field].drop_nulls().is_finite().all():
        raise ValueError(f'{index.upper()}变化缺失或非有限')
    if not context.select('date').sort('date').equals(calendar):
        raise ValueError(f'{index.upper()}上下文必须精确覆盖股票研究日历')
    source=pl.read_csv(source_path)
    expected_schema = {'DATE':pl.String,'OPEN':pl.Float64,'HIGH':pl.Float64,'LOW':pl.Float64,'CLOSE':pl.Float64} if index == 'vix' else {'DATE':pl.String,index.upper():pl.Float64}
    if source.schema != expected_schema:
        raise ValueError(f'{index.upper()}公开源CSV结构不符合冻结合同')
    source=source.select(pl.col('DATE').str.strptime(pl.Date,'%m/%d/%Y').alias('date'),pl.col('CLOSE' if index == 'vix' else index.upper()).alias('vix_close'))
    if source['date'].null_count() or source['date'].n_unique()!=source.height:
        raise ValueError(f'{index.upper()}公开源日期缺失或重复')
    anchor=date.fromisoformat(contract['previous_session_date'])
    if anchor>=calendar['date'].min():
        raise ValueError('首日前置日必须早于研究日历')
    extended=pl.concat([pl.DataFrame({'date':[anchor]}),calendar]).join(source,on='date',how='left',validate='1:1').sort('date')
    if index == 'ovx':
        declared = contract.get('source_missing_dates')
        actual_missing = [d.isoformat() for d in extended.filter(pl.col('vix_close').is_null())['date']]
        if not isinstance(declared, list) or declared != actual_missing:
            raise ValueError('OVX来源缺口必须在读取结果前逐日登记，不允许新增未知缺口')
    if (index != 'ovx' and extended['vix_close'].null_count()) or not extended['vix_close'].drop_nulls().is_finite().all() or (extended['vix_close']<=0).any():
        raise ValueError(f'{index.upper()}指定端点缺失、非有限或非正，不允许顺延或填补')
    derived=extended.with_columns(pl.col('vix_close').diff().alias(field)).tail(calendar.height).select('date',field)
    if not derived.equals(context.sort('date')):
        raise ValueError(f'{index.upper()}变化不等于冻结来源在精确股票日历端点的点数差')
    if frame.select('date').unique().join(context.lazy().select('date'),on='date',how='anti').limit(1).collect().height:
        raise ValueError(f'股票面板存在{index.upper()}上下文之外的日期')
    return frame.join(context.lazy(),on='date',how='left',validate='m:1')
