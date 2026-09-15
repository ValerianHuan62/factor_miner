"""同业指数不能包含自身发行人，不能前填分类或偷用当日权重。"""
from datetime import date
import json
import polars as pl
import pytest
from factor_miner.industry_research import attach_classification, industry_context, publish_industry_release
from factor_miner.ridge_strategy import file_hash


def panel():
    days = [date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7)]
    rows = [dict(date=d, asset=a, issuer_id=firm, industry_code=20, market_cap_usd=cap,
                 stock_price_return=ret, valid_for_factor_rank=True)
            for d in days for a, firm, cap, ret in [('A1','A',40.,.8),('A2','A',60.,.9),
                 ('B','B',80.,.1),('C','C',60.,.2),('D','D',40.,.3),('E','E',20.,.4)]]
    return pl.DataFrame(rows), pl.DataFrame({'date': days})


def context(frame, calendar):
    return industry_context(frame, calendar, min_peer_issuers=2, min_leader_issuers=2, leader_fraction=.5, min_known_weight=1.)


def test_self_issuer_exclusion_and_previous_day_weights():
    frame, calendar = panel()
    result = context(frame, calendar)
    target = result.filter((pl.col('date') == date(2025,1,6)) & (pl.col('asset') == 'A1'))
    assert target['industry_leader_return'].item() == pytest.approx((80*.1+60*.2)/140)
    assert target['industry_peer_count'].item() == 4
    assert result.filter(pl.col('date') == date(2025,1,3))['industry_peer_return'].null_count() == 6
    changed = frame.with_columns(pl.when(pl.col('issuer_id') == 'A').then(100.).otherwise(pl.col('stock_price_return')).alias('stock_price_return'))
    altered = context(changed, calendar).filter((pl.col('date') == date(2025,1,6)) & (pl.col('asset') == 'A1'))
    assert altered['industry_leader_return'].item() == pytest.approx(target['industry_leader_return'].item())
    changed = frame.with_columns(pl.when(pl.col('date') >= date(2025,1,6)).then(1e12).otherwise(pl.col('market_cap_usd')).alias('market_cap_usd'))
    assert context(changed, calendar).filter(pl.col('date') <= date(2025,1,6)).equals(result.filter(pl.col('date') <= date(2025,1,6)))


def test_missing_calendar_day_cap_not_shifted_and_missing_identity_unknown():
    frame, calendar = panel()
    frame = frame.filter(~((pl.col('asset') == 'B') & (pl.col('date') == date(2025,1,6))))
    result = context(frame, calendar)
    assert result.filter((pl.col('date') == date(2025,1,7)) & (pl.col('asset') == 'A1'))['industry_peer_count'].item() == 3
    frame = frame.with_columns(pl.when(pl.col('asset') == 'A1').then(None).otherwise(pl.col('issuer_id')).alias('issuer_id'))
    assert context(frame, calendar).filter(pl.col('asset') == 'A1')['industry_leader_return'].null_count() == 3


def test_classification_uses_effective_intervals_and_rejects_conflicts():
    keys = pl.DataFrame({'date':[date(2024,1,2),date(2025,1,2)],'asset':['A','A']})
    master = pl.DataFrame({'asset':['A'],'valid_from':[date(2025,1,1)],'valid_to':[date(2025,12,31)],'sic_code':[2010],'issuer_id':['firm']})
    result = attach_classification(keys, master)
    assert result['industry_code'].to_list() == [None,20]
    conflict = pl.concat([master, master.with_columns(pl.lit('other').alias('issuer_id'))])
    with pytest.raises(ValueError, match='冲突'):
        attach_classification(keys, conflict)


def test_publisher_preserves_original_panels(tmp_path):
    frame, calendar = panel()
    base = tmp_path/'base'; base.mkdir()
    market = frame.select('date','asset','market_cap_usd','stock_price_return').with_columns(pl.lit(100.).alias('open'))
    market.write_parquet(base/'market.parquet')
    frame.select('date','asset','valid_for_factor_rank').write_parquet(base/'state.parquet')
    calendar.write_parquet(base/'calendar.parquet')
    for name in ['label.parquet','terminal_values.parquet']:
        frame.select('date','asset').write_parquet(base/name)
    hashes = {p.name:file_hash(p) for p in base.iterdir()}
    (base/'manifest.json').write_text(json.dumps(dict(files=hashes)))
    release = tmp_path/'release.json'
    release.write_text(json.dumps(dict(market_id='us_equity',release_id='synthetic',source='合成',adjustment='split-adjusted price-only',
        calendar_version=hashes['calendar.parquet'],state_version=hashes['state.parquet'],as_of_date='2025-01-07',state_as_of_date='2025-01-07')))
    master = tmp_path/'master.parquet'
    frame.select('asset','issuer_id').unique().with_columns(pl.lit(date(2020,1,1)).alias('valid_from'),pl.lit(date(2025,12,31)).alias('valid_to'),pl.lit(2000).alias('sic_code')).write_parquet(master)
    c=dict(output_root=str(tmp_path/'run'),base_dataset_root=str(base),base_release_path=str(release),master_path=str(master),
        availability='after_close_t',revision_policy='annual_historical_release_no_daily_vintage_guarantee',release_id='industry',
        master_columns={n:n for n in ['asset','issuer_id','valid_from','valid_to','sic_code']},invalid_issuer_values=['','0','-1'],
        peer_policy=dict(min_peer_issuers=2,min_leader_issuers=2,leader_fraction=.5,min_known_weight=1.))
    c['input_sha256']={str(p):file_hash(p) for p in [*base.iterdir(),master,release]}
    path=tmp_path/'config.json';path.write_text(json.dumps(c))
    out=publish_industry_release(path)
    assert pl.read_parquet(out/'dataset/market.parquet').select(market.columns).equals(market)
    for name in ['state.parquet','calendar.parquet','label.parquet','terminal_values.parquet']:
        assert file_hash(out/'dataset'/name) == hashes[name]
    with pytest.raises(FileExistsError):publish_industry_release(path)


def test_industry_strength_and_lag_beta_have_independent_response_checks():
    from factor_miner.schema import FactorNode as N
    from factor_miner.construct_validation import ObservableCondition, validate_construct
    from factor_miner.dsl import validate_ast
    leader=N(op='field',field='industry_leader_return')
    strength=N(op='rolling_mean',args=(leader,),window=20)
    beta=N(op='rolling_partial_beta',args=(N(op='field',field='stock_price_return'),
        N(op='calendar_delay',args=(leader,),period=1),N(op='field',field='industry_peer_return')),window=60)
    for node,test in [(strength,'industry_leader_strength'),(beta,'industry_lag_beta')]:
        condition=ObservableCondition(observation='行业传播',measurement=test,response_test=test)
        assert validate_construct(node,condition)['status']=='基础检验符合'
        assert validate_construct(node,condition.model_copy(update={'expected_response':'decrease'}))['status']!='基础检验符合'
        assert validate_ast(node,allowed_fields={'industry_leader_return','industry_peer_return','stock_price_return'},forbidden_fields=()).depth<=5
