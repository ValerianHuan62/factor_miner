"""报价身份、无未来连接和原面板不变的合成回归。"""
from datetime import date
import json

import polars as pl
import pytest

from factor_miner.quote_research import normalize_quotes, attach_prior_quotes, publish_quote_release, audit_quote_costs
from factor_miner.ridge_strategy import file_hash
from factor_miner.research_report import write_json


def quotes():
    return pl.DataFrame({'date':[date(2025,1,3),date(2025,1,6),date(2025,1,7)],'asset':['A']*3,
        'quote_bid_raw':[99.,98.,97.], 'quote_ask_raw':[101.,102.,103.], 'quote_close_raw':[100.]*3})


def test_identical_duplicates_crossed_quotes_and_zero_spread():
    source=quotes();normalized,report=normalize_quotes(pl.concat([source,source.head(1)]))
    assert normalized.height==3 and report['identical_duplicate_keys']==1
    with pytest.raises(ValueError,match='冲突报价'):
        normalize_quotes(pl.concat([source,source.head(1).with_columns(pl.lit(98.).alias('quote_bid_raw'))]))
    malformed=source.with_columns(pl.Series('quote_bid_raw',[102.,0.,100.]),pl.Series('quote_ask_raw',[101.,1.,100.]))
    result,_=normalize_quotes(malformed)
    assert result['valid_closing_quote'].to_list()==[False,False,True]
    assert result['quote_bid_raw'].to_list()==[None,None,100.]
    assert result['valid_closing_quote_position'].to_list()==[False,False,False]


def test_previous_market_day_quotes_and_missing_quote_remain_unknown():
    source,_=normalize_quotes(quotes());calendar=pl.DataFrame({'date':[date(2025,1,3),date(2025,1,6),date(2025,1,7)]})
    orders=pl.DataFrame({'actual_date':[date(2025,1,6),date(2025,1,7)],'security_id':['A','A'],'gross_notional':[1.,2.]})
    result=attach_prior_quotes(orders,source,calendar).sort('actual_date')
    assert result['prior_quote_half_spread_bps'].to_list()==[100.,200.]
    altered=source.with_columns(pl.when(pl.col('date')==date(2025,1,7)).then(120.).otherwise(pl.col('quote_ask_raw')).alias('quote_ask_raw'))
    assert attach_prior_quotes(orders,altered,calendar).sort('actual_date')['prior_quote_half_spread_bps'].equals(result['prior_quote_half_spread_bps'])
    missing=attach_prior_quotes(orders,source.filter(pl.col('date')!=date(2025,1,6)),calendar).sort('actual_date')
    assert missing['prior_quote_half_spread_bps'].to_list()==[100.,None]
    assert missing['gross_notional'].sum()==3.


def test_release_preserves_all_original_inputs_and_rejects_overwrite(tmp_path):
    base=tmp_path/'base';base.mkdir();data=quotes()
    market=data.select('date','asset').with_columns(pl.lit(100.).alias('open'));market.write_parquet(base/'market.parquet')
    for name in ['state.parquet','calendar.parquet','label.parquet','terminal_values.parquet']:
        data.select('date','asset').write_parquet(base/name)
    names=['market.parquet','state.parquet','calendar.parquet','label.parquet','terminal_values.parquet'];hashes={n:file_hash(base/n) for n in names}
    write_json(base/'manifest.json',dict(files=hashes))
    release=tmp_path/'release.json';write_json(release,dict(market_id='us_equity',release_id='base',source='合成',adjustment='split-adjusted price-only',calendar_version=hashes['calendar.parquet'],state_version=hashes['state.parquet'],as_of_date='2025-01-07',state_as_of_date='2025-01-07'))
    source=tmp_path/'source.parquet';data.write_parquet(source)
    config=dict(output_root=str(tmp_path/'run'),base_dataset_root=str(base),base_release_path=str(release),source_path=str(source),
        quote_price_basis='unadjusted_same_day_usd',availability='after_close_t',source_columns={n:n for n in data.columns},asset_prefix='',release_id='quotes',revision_policy='合成测试',
        input_sha256={str(p):file_hash(p) for p in [*(base/n for n in [*names,'manifest.json']),release,source]})
    path=tmp_path/'config.json';write_json(path,config);output=publish_quote_release(path)
    assert pl.read_parquet(output/'dataset/market.parquet').select(market.columns).equals(market)
    assert all(file_hash(base/n)==hashes[n] for n in names)
    assert all(file_hash(output/'dataset'/n)==hashes[n] for n in names[1:])
    assert json.loads((output/'verification.json').read_text())['labels_read'] is False
    with pytest.raises(FileExistsError):publish_quote_release(path)


def test_cost_audit_preserves_missing_notional_and_does_not_reprice(tmp_path):
    data,_=normalize_quotes(quotes().filter(pl.col('date')!=date(2025,1,6)))
    market=tmp_path/'market.parquet';data.write_parquet(market)
    calendar=tmp_path/'calendar.parquet';quotes().select('date').write_parquet(calendar)
    orders=tmp_path/'orders.parquet'
    pl.DataFrame({'actual_date':[date(2025,1,6),date(2025,1,7)],'security_id':['A','A'],
        'gross_notional':[1.,2.],'status':['filled']*2,'side':['buy','sell']}).write_parquet(orders)
    digest=file_hash(orders)
    c=dict(output_root=str(tmp_path/'audit'),market_path=str(market),calendar_path=str(calendar),orders={'baseline':str(orders)},one_way_cost_bps=10.,input_sha256={str(p):file_hash(p) for p in [market,calendar,orders]})
    path=tmp_path/'config.json';write_json(path,c);root=audit_quote_costs(path)
    report=json.loads((root/'summary.json').read_text());row=report['rows'][0]
    assert row['notional_coverage']==pytest.approx(1/3)
    assert row['missing_quote_notional']==2.
    assert row['notional_weighted_half_spread_bps']==100.
    assert row['notional_fraction_above_fixed_cost']==1.
    assert report['return_path_recomputed'] is False and file_hash(orders)==digest
