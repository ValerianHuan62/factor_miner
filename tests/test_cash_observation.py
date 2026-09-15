"""历史现金观察的单位、零值、缺失、固定日历及不可覆盖验证。"""
from datetime import date
import json
import polars as pl
import pytest
from factor_miner.cash_observation import build_cash_observations, build_cash_observation_prototype
from factor_miner.research_report import write_json
from factor_miner.ridge_strategy import file_hash


def inputs():
    days=[date(2025,1,3),date(2025,1,6),date(2025,1,7)]
    keys=pl.DataFrame({'date':[days[1]]*4+[days[2]],'asset':['A','B','C','D','A']})
    source=pl.DataFrame({'date':[days[1]]*3+[days[2]],'asset':['A','B','C','A'],
        'ordinary_amount':[1.,0.,None,1.], 'previous_price':[100.]*4,
        'previous_date':[days[0]]*4, 'vendor_income_return':[.01,0.,None,.01]})
    return keys,source,pl.DataFrame({'date':days})


def test_preserves_zero_missing_and_nonconsecutive_price_period():
    keys,source,calendar=inputs();out,report=build_cash_observations(keys,source,calendar)
    assert out['cash_observation_status'].to_list()==['positive','zero','unknown','unknown','unknown']
    assert out['ordinary_cash_income_fraction'].to_list()==[.01,0.,None,None,None]
    assert out['cash_unknown_reason'].to_list()==['','','invalid_amount','source_missing','nonconsecutive_price_period']
    assert report['stock_universe_unchanged'] and report['comparable_rows']==2
    # 股份单位变化同时作用于分配额和前期价格，收入比例应保持不变。
    scaled=source.with_columns((pl.col('ordinary_amount')*10).alias('ordinary_amount'),(pl.col('previous_price')*10).alias('previous_price'))
    changed,_=build_cash_observations(keys,scaled,calendar)
    assert changed['ordinary_cash_income_fraction'].equals(out['ordinary_cash_income_fraction'])


def test_rejects_conflicts_future_dates_and_wrong_share_basis():
    keys,source,calendar=inputs()
    _,r=build_cash_observations(keys,pl.concat([source,source.head(1)]),calendar)
    assert r['identical_duplicate_keys']==1
    with pytest.raises(ValueError,match='冲突'):
        build_cash_observations(keys,pl.concat([source,source.head(1).with_columns(pl.lit(2.).alias('ordinary_amount'))]),calendar)
    with pytest.raises(ValueError,match='当前或未来'):
        build_cash_observations(keys,source.with_columns(pl.col('date').alias('previous_date')),calendar)
    with pytest.raises(ValueError,match='股份单位'):
        build_cash_observations(keys,source.with_columns((pl.col('ordinary_amount')*2).alias('ordinary_amount')),calendar)


def test_prototype_preserves_market_and_has_no_candidate_or_label_outputs(tmp_path):
    keys,source,calendar=inputs();market=tmp_path/'market.parquet';keys.with_columns(pl.lit(100.).alias('open')).write_parquet(market)
    src=tmp_path/'source.parquet';source.write_parquet(src);cal=tmp_path/'calendar.parquet';calendar.write_parquet(cal)
    state=tmp_path/'state.parquet';keys.with_columns(pl.lit(True).alias('valid_for_factor_rank')).write_parquet(state)
    manifest=tmp_path/'manifest.json';write_json(manifest,dict(as_of_date='2025-01-07',files={p.name:file_hash(p) for p in [market,cal,state]}))
    paths=[market,src,cal,state,manifest];before={str(p):file_hash(p) for p in paths}
    config=dict(output_root=str(tmp_path/'run'),source_path=str(src),market_path=str(market),calendar_path=str(cal),state_path=str(state),base_manifest_path=str(manifest),input_sha256=before,
        availability='after_close_t',amount_basis='previous_price_period_share_basis',source_columns={n:n for n in source.columns},asset_prefix='',unit_tolerance=1e-6,revision_policy='合成')
    path=tmp_path/'config.json';write_json(path,config);root=build_cash_observation_prototype(path)
    assert all(file_hash(p)==before[str(p)] for p in paths)
    summary=json.loads((root/'summary.json').read_text());assert summary['candidates_registered']==0 and summary['prototype_only']
    assert not (root/'label.parquet').exists() and not (root/'ledger').exists()
    assert pl.read_parquet(root/'cash_observations.parquet').height==keys.height
    with pytest.raises(FileExistsError):build_cash_observation_prototype(path)
