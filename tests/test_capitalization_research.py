"""市值单位、原面板不变与换手构念的独立扰动回归。"""
from datetime import date
import json
import polars as pl
import pytest
from factor_miner.capitalization_research import normalize_capitalization,publish_capitalization_release
from factor_miner.construct_validation import ObservableCondition,validate_construct
from factor_miner.schema import FactorNode
from factor_miner.ridge_strategy import file_hash
from factor_miner.research_report import write_json


def source():
    return pl.DataFrame({'date':[date(2025,1,3),date(2025,1,6),date(2025,1,7)],'asset':['A']*3,'market_cap_usd':[1e6,1e6,0.], 'capitalization_price_raw':[100.]*3,'capitalization_volume_raw':[100.,0.,100.]})


def test_zero_turnover_is_valid_but_unknown_or_zero_cap_is_not():
    x=source();out,report=normalize_capitalization(pl.concat([x,x.head(1)]))
    assert report['identical_duplicate_keys']==1 and report['invalid_keys']==1
    assert out['valid_capitalization_observation'].to_list()==[True,True,False]
    assert out['capitalization_volume_raw'].to_list()==[100.,0.,None]
    with pytest.raises(ValueError,match='冲突'):
        normalize_capitalization(pl.concat([x,x.head(1).with_columns(pl.lit(2e6).alias('market_cap_usd'))]))


def test_turnover_requires_share_scaling_and_correct_response():
    def f(n):return FactorNode(op='field',field=n)
    turnover=FactorNode(op='div',args=(FactorNode(op='mul',args=(f('capitalization_volume_raw'),f('capitalization_price_raw'))),f('market_cap_usd')))
    level=FactorNode(op='rolling_mean',args=(turnover,),window=20)
    returns=FactorNode(op='div',args=(f('close'),FactorNode(op='calendar_delay',args=(f('close'),),period=1)))
    coupling=FactorNode(op='rolling_cov',args=(returns,turnover),window=20)
    for node,test in [(level,'turnover_level'),(coupling,'turnover_return_coupling')]:
        condition=ObservableCondition(observation='换手状态',measurement='同日成交价值除以市值',response_test=test)
        assert validate_construct(node,condition)['status']=='基础检验符合'
        assert validate_construct(node,condition.model_copy(update={'expected_response':'decrease'}))['status']!='基础检验符合'
    bad=FactorNode(op='div',args=(f('capitalization_volume_raw'),f('market_cap_usd')))
    assert validate_construct(bad,ObservableCondition(observation='换手',measurement='换手',response_test='turnover_level'))['status']!='基础检验符合'


@pytest.mark.parametrize("include_context",[False,True])
def test_release_converts_thousands_and_does_not_modify_labels(tmp_path,include_context):
    base=tmp_path/'base';base.mkdir();data=source()
    market=data.select('date','asset').with_columns(pl.lit(100.).alias('open'));market.write_parquet(base/'market.parquet')
    data.select('date','asset').with_columns(pl.lit(True).alias('valid_for_factor_rank')).write_parquet(base/'state.parquet')
    for n in ['calendar.parquet','label.parquet','terminal_values.parquet']:data.select('date','asset').write_parquet(base/n)
    names=['market.parquet','state.parquet','calendar.parquet','label.parquet','terminal_values.parquet'];hashes={n:file_hash(base/n) for n in names}
    write_json(base/'manifest.json',dict(files=hashes));release=tmp_path/'release.json'
    write_json(release,dict(market_id='us_equity',release_id='base',source='合成',adjustment='split-adjusted price-only',calendar_version=hashes['calendar.parquet'],state_version=hashes['state.parquet'],as_of_date='2025-01-07',state_as_of_date='2025-01-07'))
    raw=tmp_path/'source.parquet'
    if include_context:
        data=data.with_columns(pl.Series('reported_price_return',[.1,.2,.3]),pl.Series('previous_price_date',[date(2025,1,2),date(2025,1,3),date(2025,1,6)]),pl.Series('leader_return',[.01,.02,.03]))
    data.write_parquet(raw)
    c=dict(output_root=str(tmp_path/'run'),base_dataset_root=str(base),base_release_path=str(release),source_path=str(raw),availability='after_close_t',capitalization_unit='thousand_usd',price_volume_basis='unadjusted_same_day',revision_policy='annual_historical_release_no_daily_vintage_guarantee',source_columns={n:n for n in source().columns},asset_prefix='',release_id='cap',input_sha256={str(p):file_hash(p) for p in [*(base/n for n in [*names,'manifest.json']),release,raw]})
    if include_context:c.update(include_return_context=True,return_columns={n:n for n in ['reported_price_return','previous_price_date','leader_return']},leader_definition='crsp_value_weighted_price_return')
    config=tmp_path/'config.json';write_json(config,c);out=publish_capitalization_release(config)
    panel=pl.read_parquet(out/'dataset/market.parquet')
    if include_context:assert panel['stock_price_return'].to_list()==[None,.2,.3]
    assert panel['market_cap_usd'][0]==1e9 and panel['capitalization_volume_raw'][1]==0
    assert panel.select(market.columns).equals(market)
    assert all(file_hash(base/n)==hashes[n] for n in names)
    assert all(file_hash(out/'dataset'/n)==hashes[n] for n in names[1:])
    assert json.loads((out/'verification.json').read_text())['labels_read'] is False
    with pytest.raises(FileExistsError):publish_capitalization_release(config)


def test_return_context_uses_fixed_calendar_and_checks_index_consistency():
    from factor_miner.capitalization_research import normalize_return_context
    days=[date(2025,1,3),date(2025,1,6),date(2025,1,7)]
    calendar=pl.DataFrame({'date':days})
    raw=pl.DataFrame({'date':days,'asset':['A']*3,'reported_price_return':[.1,.2,.3], 'previous_price_date':[date(2025,1,2),days[0],days[0]],'leader_return':[.01,.02,.03]})
    x=normalize_return_context(raw,calendar)
    assert x['stock_price_return'].to_list()==[None,.2,None]
    bad=raw.with_columns(pl.col('date').alias('previous_price_date'))
    with pytest.raises(ValueError,match='未来'):normalize_return_context(bad,calendar)
    with pytest.raises(ValueError,match='指数记录'):
        normalize_return_context(pl.concat([raw,raw.with_columns(pl.lit('B').alias('asset'),pl.lit(.8).alias('leader_return'))]),calendar)


def test_size_and_leader_catchup_fit_existing_dsl_limits():
    from factor_miner.dsl import validate_ast
    def f(n):return FactorNode(op='field',field=n)
    leader=f('leader_return')
    beta=FactorNode(op='rolling_partial_beta',args=(f('stock_price_return'),FactorNode(op='calendar_delay',args=(leader,),period=1),leader),window=60)
    catchup=FactorNode(op='mul',args=(beta,FactorNode(op='rolling_mean',args=(leader,),window=5)))
    for node,test in [(f('market_cap_usd'),'capitalization_size'),(catchup,'leader_catchup')]:
        assert validate_construct(node,ObservableCondition(observation='规模与信息扩散',measurement=test,response_test=test))['status']=='基础检验符合'
    assert validate_ast(catchup,allowed_fields={'stock_price_return','leader_return'},forbidden_fields=()).depth<=5
