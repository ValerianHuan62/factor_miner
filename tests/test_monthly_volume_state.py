"""月度状态的因果性、缺失语义、并列值及正式预检边界。"""
from datetime import date
import json

import numpy as np
import polars as pl
import pytest

from factor_miner.monthly_volume_state import (MonthlyVolumeGamma, MonthlyVolumePreflightPlan,
    raw_monthly_volume, monthly_volume_states, monthly_state_feasibility)
from factor_miner.monthly_volume_synthetic import synthetic_monthly_panel, inspect_monthly_volume_contract
from factor_miner.favor_preflight import preflight_favor
from factor_miner.favor_demo import make_plan


G = MonthlyVolumeGamma()


def state_fixture(values):
    rows = []
    # a 的排名由传入数值决定；9 个固定同伴保持全市场分母。
    for i, value in enumerate(values):
        d = date(2000+i//12, i%12+1, 28)
        for j in range(10):
            rows.append(dict(date=d, asset=str(j), month_id=2000*12+i, atv=value if j == 0 else float(j), universe_member=True))
    return pl.DataFrame(rows)


def plan_fixture(tmp_path):
    (tmp_path / "old").mkdir()
    old, _ = make_plan(tmp_path / "old")
    return MonthlyVolumePreflightPlan(version="favor-monthly-state-preflight-v1", hypothesis=old.hypothesis,
        gamma=G, dataset_root=str(tmp_path / "dataset"), data_contract_path=str(tmp_path / "release.json"),
        source_protocol_path=str(tmp_path / "protocol.json"), input_sha256={}, discovery_start=date(2001, 1, 1),
        discovery_end=date(2002, 12, 31), historical_attempt_checkpoint=195, diagnostic_family_floor=16190)


def test_synthetic_invariants():
    assert inspect_monthly_volume_contract(G)["passed"]


def test_run_is_unknown_after_missing_until_reset_or_cap():
    raw = state_fixture([4., 20., 20., None, 20., 20., 20., 20., 20., 4., 20.])
    result = monthly_volume_states(raw, G).filter(pl.col("asset") == "0")
    assert result["patv"].to_list() == [0, 1, 2, None, None, None, None, None, 5, 0, 1]
    first = monthly_volume_states(state_fixture([20.]*6), G).filter(pl.col("asset") == "0")
    assert first["patv"].to_list() == [None, None, None, None, 5, 5]


def test_missing_month_does_not_compress_time_or_reorder_ties():
    raw = state_fixture([4., 20., 20., 20.])
    raw = raw.filter(~((pl.col("asset") == "0") & (pl.col("month_id") == 2000*12+2)))
    result = monthly_volume_states(raw, G).filter(pl.col("asset") == "0")
    assert result["patv"].to_list() == [0, 1, None]
    tied = state_fixture([9., 9., 9.])  # 与最高同伴并列，二者始终同组。
    a = monthly_volume_states(tied, G)
    b = monthly_volume_states(tied.reverse(), G)
    assert a.equals(b)
    assert a.filter(pl.col("asset").is_in(["0", "9"]))["extreme"].to_list() == [1]*6


def test_same_marginal_values_different_order_changes_persistence():
    a = monthly_volume_states(state_fixture([4., 20., 20., 4.]), G).filter(pl.col("asset") == "0")
    b = monthly_volume_states(state_fixture([4., 20., 4., 20.]), G).filter(pl.col("asset") == "0")
    assert a["patv"].to_list() == [0, 1, 2, 0]
    assert b["patv"].to_list() == [0, 1, 0, 1]


def test_missing_session_invalidates_month_and_baseline(tmp_path):
    market, state, calendar = synthetic_monthly_panel()
    market = market.filter(~((pl.col("asset") == "a") & (pl.col("date") == date(2000, 2, 3))))
    raw = raw_monthly_volume(market, state, calendar, G, through=date(2002, 12, 31))
    a = raw.filter(pl.col("asset") == "a")
    assert a.filter(pl.col("month_id") == 2000*12+1)["monthly_volume"].item() is None
    assert a.filter(pl.col("month_id") == 2000*12+18)["atv"].item() is None
    b = raw.filter((pl.col("asset") == "b") & (pl.col("month_id") == 2000*12+18))
    assert np.isclose(b["atv"].item(), np.log(118/105.5))
    with pytest.raises(ValueError, match="对应状态"):
        raw_monthly_volume(market, state.slice(1), calendar, G, through=date(2002, 12, 31))


def test_no_trigger_stocks_remain_in_denominator(tmp_path):
    p = plan_fixture(tmp_path).model_copy(update={"discovery_start":date(2000,1,1), "discovery_end":date(2000,12,31)})
    raw = state_fixture([4.,20.]*6)
    result = monthly_state_feasibility(raw, monthly_volume_states(raw, G), p)
    assert result["universe_tickers"] == 10
    assert result["sufficient_tickers"] == 2
    assert result["support_upper_bound"] == .2
    assert result["identical_ladder_by_definition"]
    assert not result["feasible"] and not result["return_labels_used"]


def test_schema_rejects_relaxed_gates_and_arbitrary_fields(tmp_path):
    payload = plan_fixture(tmp_path).model_dump(mode="json")
    with pytest.raises(ValueError):MonthlyVolumePreflightPlan.model_validate({**payload,"support_threshold":.1})
    with pytest.raises(ValueError):MonthlyVolumeGamma(volume_field="label_o2o_5d")
    with pytest.raises(ValueError):MonthlyVolumeGamma(cap_months=3)


def test_formal_synthetic_preflight_freezes_gamma_without_opening_data(tmp_path):
    p = plan_fixture(tmp_path)
    path = tmp_path/"plan.json";path.write_text(p.model_dump_json())
    output=tmp_path/"preflight.json"
    result=preflight_favor(path,output)
    assert result["passed"] and not result["return_labels_used"]
    report=json.loads(output.read_text())
    assert report["registered_candidates"] == 0 and not report["real_market_data_used"]
    assert json.loads((output.with_suffix('.monthly_state')/'gamma.json').read_text())["gamma_sha256"] == G.identity
    with pytest.raises(FileExistsError):preflight_favor(path,output)


def test_invalid_release_preserves_failure(tmp_path):
    p = plan_fixture(tmp_path)
    path = tmp_path/"plan.json";path.write_text(p.model_dump_json())
    output=tmp_path/"preflight.json"
    with pytest.raises(FileNotFoundError):preflight_favor(path,output,with_data=True)
    assert not output.exists()
    failure=json.loads((output.with_suffix('.monthly_state')/'failure.json').read_text())
    assert not failure["return_labels_used"]


def test_complete_release_preflight_cannot_read_labels(tmp_path, monkeypatch):
    from factor_miner.favor_workflow import file_sha
    p = plan_fixture(tmp_path)
    dataset = tmp_path / 'dataset';dataset.mkdir()
    market, state, calendar = synthetic_monthly_panel()
    for name, frame in [('market',market),('state',state),('calendar',calendar)]:
        frame.write_parquet(dataset/f'{name}.parquet')
    (dataset/'label.parquet').write_bytes('读取此文件必然失败'.encode())
    release=dict(market_id='us_equity',release_id='test',source='合成数据',adjustment='split-adjusted price-only',
        calendar_version=file_sha(dataset/'calendar.parquet'),state_version=file_sha(dataset/'state.parquet'),
        as_of_date='2002-12-31',state_as_of_date='2002-12-31')
    (tmp_path/'release.json').write_text(json.dumps(release))
    (tmp_path/'protocol.json').write_text(json.dumps(dict(release_id='test',price_volume_basis='unadjusted_same_day')))
    manifest=dict(release_id='test',as_of_date='2002-12-31',files={f'{name}.parquet':{'sha256':file_sha(dataset/f'{name}.parquet')} for name in ('market','state','calendar')})
    (dataset/'manifest.json').write_text(json.dumps(manifest))
    files=[dataset/f'{name}.parquet' for name in ('market','state','calendar')]+[dataset/'manifest.json',tmp_path/'release.json',tmp_path/'protocol.json']
    p=p.model_copy(update={'input_sha256':{str(path):file_sha(path) for path in files}})
    config=tmp_path/'plan.json';config.write_text(p.model_dump_json());output=tmp_path/'preflight.json'
    original=pl.scan_parquet; reads=[]
    def scan(path,*args,**kwargs):
        assert 'label' not in str(path)
        reads.append(str(path));return original(path,*args,**kwargs)
    monkeypatch.setattr(pl,'scan_parquet',scan)
    result=preflight_favor(config,output,with_data=True)
    assert result['passed'] and result['needs_review']
    report=json.loads(output.read_text())
    assert report['real_market_data_used'] and not report['return_labels_used']
    assert report['data_feasibility']['identical_ladder_by_definition']
    assert any('market.parquet' in path for path in reads)
    assert report['artifacts']['raw_monthly.parquet']['bytes'] > 0


def test_truncated_calendar_cannot_certify_its_last_month():
    market, state, calendar = synthetic_monthly_panel()
    end=date(2002,7,15)
    calendar=calendar.filter(pl.col('date')<=end)
    raw=raw_monthly_volume(market.filter(pl.col('date')<=end),state.filter(pl.col('date')<=end),calendar,G,through=end)
    assert raw['date'].max()==date(2002,6,28)


def test_observation_plan_is_not_a_candidate_execution_plan(tmp_path):
    from factor_miner.favor_schema import parse_favor_plan
    with pytest.raises(ValueError):
        parse_favor_plan(plan_fixture(tmp_path).model_dump(mode='json'))
