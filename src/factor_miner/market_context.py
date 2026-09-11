"""只读、显式冻结的同日市场价格收益上下文。"""
from pathlib import Path
import hashlib
import json
import polars as pl


def attach_market_context(frame: pl.LazyFrame, calendar_path: Path, context_path: Path, contract_path: Path) -> pl.LazyFrame:
    """核验上下文身份与完整覆盖后按日期连接，不填值、不改变证券集合。"""
    contract = json.loads(contract_path.read_text())
    expected = {'version':'market-context-v1', 'field':'market_return',
        'observation':'close_t', 'availability':'after_close_t',
        'return_definition':'close_t/previous_market_close-1',
        'price_basis':'price_return_excluding_dividends'}
    if any(contract.get(key)!=value for key,value in expected.items()):
        raise ValueError('市场上下文合同不支持或可得时点不明确')
    for path,key in [(context_path,'data_sha256'),(calendar_path,'calendar_sha256')]:
        if hashlib.sha256(path.read_bytes()).hexdigest()!=contract.get(key):
            raise ValueError('市场上下文或日历身份发生变化')
    if 'market_return' in frame.collect_schema():
        raise ValueError('原市场面板不得覆盖显式市场收益字段')
    context=pl.read_parquet(context_path)
    if context.schema!={'date':pl.Date,'market_return':pl.Float64}:
        raise ValueError('市场上下文字段必须为 date 与 market_return')
    if context['date'].null_count() or context['date'].n_unique()!=context.height:
        raise ValueError('市场上下文日期缺失或重复')
    if context['market_return'].null_count() or not context['market_return'].is_finite().all():
        raise ValueError('市场上下文收益缺失或非有限')
    calendar=pl.read_parquet(calendar_path).select('date')
    if calendar.is_empty() or calendar['date'].null_count() or calendar['date'].n_unique()!=calendar.height:
        raise ValueError('市场上下文日历不完整或重复')
    if calendar.join(context.select('date'),on='date',how='anti').height or context.select('date').join(calendar,on='date',how='anti').height:
        raise ValueError('市场上下文日期必须精确覆盖冻结日历')
    if frame.select('date').unique().join(context.lazy().select('date'),on='date',how='anti').limit(1).collect().height:
        raise ValueError('股票面板存在市场上下文之外的日期')
    return frame.join(context.lazy(),on='date',how='left',validate='m:1')
