"""股份事件乘数、断链、只读行情及预热行的合成回归。"""
from datetime import date, timedelta
import json
from pathlib import Path

import polars as pl
import pytest

from factor_miner.share_observation import normalize_share_observations, publish_share_observation_release, SHARE_FIELDS, EVENT_FIELDS
from factor_miner.favor_schema import FavorDataRelease
from factor_miner.ridge_strategy import file_hash


def fixture():
    days = [date(2019, 12, 27) + timedelta(days=i) for i in range(10)]
    rows = []
    for asset in ('A', 'B'):
        for i, day in enumerate(days):
            event = i == 2
            rows.append(dict(date=day, asset=asset, ShrOut=1000. if i < 2 else 2000., ShrStartDt=days[0],
                ShrEndDt=days[-1], ShrAdrFlg='N', DlyDistRetFlg='S1' if event else 'NO',
                DisExDt=day if event else None, DisSeqNbr=1 if event else None, DisFacShr=1. if event else None))
    return pl.DataFrame(rows), pl.DataFrame({'date': days})


def test_split_reverse_split_duplicate_events_and_unknown_chain():
    raw, calendar = fixture()
    split = raw.filter(pl.col('DisExDt').is_not_null())
    normalized, quality = normalize_share_observations(pl.concat([raw, split]), calendar)
    assert normalized['event_adjusted_shares'].to_list() == [1000.] * 20
    assert quality['unique_events'] == 2
    # 并股系数不能由价格复权替代；十分之一股数及-.9股份乘数应抵消。
    reverse = raw.with_columns(pl.when(pl.col('date') >= calendar['date'][2]).then(100.).otherwise(pl.col('ShrOut')).alias('ShrOut'),
        pl.when(pl.col('DisExDt').is_not_null()).then(-.9).otherwise(None).alias('DisFacShr'))
    values, _ = normalize_share_observations(reverse, calendar)
    assert values['event_adjusted_shares'].min() == pytest.approx(1000.)
    assert values['event_adjusted_shares'].max() == pytest.approx(1000.)
    unknown = raw.with_columns(pl.when(pl.col('DisExDt').is_not_null()).then(-1.).otherwise(pl.col('DisFacShr')).alias('DisFacShr'))
    values, _ = normalize_share_observations(unknown, calendar)
    assert values.filter(pl.col('date') >= calendar['date'][2])['event_adjusted_shares'].null_count() == 16
    gap = raw.filter(~((pl.col('asset') == 'A') & (pl.col('date') == calendar['date'][4])))
    values, _ = normalize_share_observations(gap, calendar)
    assert values.filter((pl.col('asset') == 'A') & (pl.col('date') > calendar['date'][4]))['event_adjusted_shares'].null_count() == 5
    changed = raw.with_columns(pl.when(pl.col('date') > calendar['date'][5]).then(12345.).otherwise(pl.col('ShrOut')).alias('ShrOut'))
    values, _ = normalize_share_observations(changed, calendar)
    assert values.filter(pl.col('date') <= calendar['date'][5]).equals(normalized.filter(pl.col('date') <= calendar['date'][5]))
    with pytest.raises(ValueError, match='乘数冲突'):
        normalize_share_observations(pl.concat([raw, split.with_columns(pl.lit(.5).alias('DisFacShr'))]), calendar)


def test_publish_preserves_base_prices_and_masks_and_uses_untradable_share_only_warmup(tmp_path):
    raw, calendar = fixture()
    raw_path = tmp_path/'raw.parquet'; raw.write_parquet(raw_path)
    base = tmp_path/'base'; base.mkdir()
    base_calendar = calendar.filter(pl.col('date') >= date(2020, 1, 1))
    market = raw.filter(pl.col('date') >= date(2020, 1, 1)).select('date', 'asset').with_columns(pl.lit(50.).alias('open'), pl.lit(51.).alias('close'))
    market.write_parquet(base/'market.parquet')
    state = market.select('date', 'asset').with_columns([pl.lit(True).alias(c) for c in ['valid_for_factor_compute', 'valid_for_factor_rank', 'can_open_long', 'can_close_long']])
    state.write_parquet(base/'state.parquet'); base_calendar.write_parquet(base/'calendar.parquet')
    # 复制字节的隔离测试：发布器不解析任何收益数值。
    (base/'label.parquet').write_bytes(b'opaque frozen labels')
    (base/'terminal_values.parquet').write_bytes(b'opaque frozen terminals')
    files = {p.name: {'sha256': file_hash(p)} for p in base.iterdir()}
    (base/'manifest.json').write_text(json.dumps({'files': files}))
    release = FavorDataRelease(market_id='us_equity', release_id='synthetic', source='synthetic', adjustment='split_adjusted',
        calendar_version=file_hash(base/'calendar.parquet'), state_version=file_hash(base/'state.parquet'),
        as_of_date=calendar['date'][-1], state_as_of_date=calendar['date'][-1])
    release_path = tmp_path/'release.json'; release_path.write_text(release.model_dump_json())
    config = dict(version='share-observation-release-v1', output_root=str(tmp_path/'output'),
        revision_policy='annual_historical_release_no_daily_vintage_guarantee', chain_policy='first_observation_anchor_unknown_breaks_permanently',
        share_unit='thousand_shares', observation_start='2019-01-01', base_dataset_root=str(base), base_release_path=str(release_path),
        source_path=str(raw_path), label_path=str(base/'label.parquet'), release_id='synthetic-shares', asset_prefix='',
        source_columns={f: f for f in ['date', 'asset', *SHARE_FIELDS, *EVENT_FIELDS]},
        input_sha256={str(p): file_hash(p) for p in [*base.iterdir(), release_path, raw_path]})
    path = tmp_path/'config.json'; path.write_text(json.dumps(config))
    root = publish_share_observation_release(path)
    output = pl.read_parquet(root/'dataset/market.parquet')
    assert output.filter(pl.col('date') >= date(2020, 1, 1)).select(market.columns).equals(market.sort('date', 'asset'))
    warm = output.filter(pl.col('date') < date(2020, 1, 1))
    assert warm['open'].null_count() == warm.height and warm['event_adjusted_shares'].null_count() == 0
    out_state = pl.read_parquet(root/'dataset/state.parquet')
    assert not out_state.filter(pl.col('date') < date(2020, 1, 1))['can_open_long'].any()
    assert out_state.filter(pl.col('date') >= date(2020, 1, 1)).equals(state.sort('date', 'asset'))
    assert (root/'dataset/label.parquet').read_bytes() == b'opaque frozen labels'
    assert json.loads((root/'verification.json').read_text())['labels_read'] is False
    assert (root/'completion.json').exists()
