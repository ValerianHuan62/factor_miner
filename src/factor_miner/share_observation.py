"""股份观察发布：独立公司行动乘数、固定市场日历及未知链条隔离。"""
from datetime import date, datetime, timezone
from pathlib import Path
import json
from factor_miner.artifact_storage import reference_panel

import polars as pl

from factor_miner.favor_schema import FavorDataRelease
from factor_miner.favor_validation import require_panel
from factor_miner.quote_research import _snapshot_code, _verify_code, _verify_inputs
from factor_miner.research_report import write_json
from factor_miner.ridge_strategy import file_hash

SHARE_FIELDS = ('ShrOut', 'ShrStartDt', 'ShrEndDt', 'ShrAdrFlg', 'DlyDistRetFlg')
EVENT_FIELDS = ('DisExDt', 'DisSeqNbr', 'DisFacShr')


def normalize_share_observations(raw: pl.DataFrame, calendar: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """只对从首次观察起连续已知的链条提供调整股数；断链后不重启基准。"""
    required = {'date', 'asset', *SHARE_FIELDS, *EVENT_FIELDS}
    if required - set(raw.columns) or raw.schema.get('date') != pl.Date:
        raise ValueError('股份观察缺少字段或日期类型错误')
    for field in ('ShrStartDt', 'ShrEndDt', 'DisExDt'):
        if raw.schema[field] != pl.Date:
            raise ValueError('股份有效期或事件日期必须为 Date')
    days = calendar['date'].to_list()
    if calendar.schema.get('date') != pl.Date or not days or None in days or days != sorted(set(days)):
        raise ValueError('股份观察需要完整唯一有序的市场日历')
    if raw.select(pl.any_horizontal(pl.col('date').is_null(), pl.col('asset').is_null()).any()).item():
        raise ValueError('股份观察主键缺失')
    if not set(raw['date'].to_list()).issubset(days):
        raise ValueError('股份观察存在日历外日期')
    conflicts = raw.group_by('date', 'asset').agg(*(pl.col(f).n_unique().alias(f) for f in SHARE_FIELDS))
    if conflicts.filter(pl.any_horizontal(*(pl.col(f) > 1 for f in SHARE_FIELDS))).height:
        raise ValueError('同日股份观察内容冲突')
    daily = raw.select('date', 'asset', *SHARE_FIELDS).unique(subset=['date', 'asset'])
    events = raw.filter(pl.col('DisExDt').is_not_null())
    if events.filter((pl.col('DisExDt') != pl.col('date')) | pl.col('DisSeqNbr').is_null()).height:
        raise ValueError('股份事件日期或序号不正确')
    if raw.filter(pl.col('DisExDt').is_null() & (pl.col('DisSeqNbr').is_not_null() | pl.col('DisFacShr').is_not_null())).height:
        raise ValueError('存在缺少事件日期的股份调整记录')
    conflicts = events.group_by('asset', 'DisExDt', 'DisSeqNbr').agg(pl.col('DisFacShr').n_unique().alias('n'))
    if conflicts.filter(pl.col('n') > 1).height:
        raise ValueError('同一公司行动的股份乘数冲突')
    events = events.unique(subset=['asset', 'DisExDt', 'DisSeqNbr'])
    events = events.with_columns((pl.col('DisFacShr').is_finite() & (pl.col('DisFacShr') > -1)).fill_null(False).alias('known'))
    grouped = events.group_by('date', 'asset').agg(pl.len().alias('n'), pl.col('known').all().alias('known'),
        (1 + pl.col('DisFacShr')).product().alias('multiplier'), (pl.col('DisFacShr') != 0).sum().alias('share_events'))
    daily = daily.join(grouped, on=['date', 'asset'], how='left', validate='1:1')
    no_event = (pl.col('DlyDistRetFlg') == 'NO') & pl.col('n').is_null()
    simple = no_event | (pl.col('known') & (
        ((pl.col('DlyDistRetFlg') == 'C1') & (pl.col('n') == 1) & (pl.col('share_events') == 0)) |
        ((pl.col('DlyDistRetFlg') == 'S1') & (pl.col('n') == 1) & (pl.col('share_events') == 1)) |
        ((pl.col('DlyDistRetFlg') == 'CS') & (pl.col('n') == 2) & (pl.col('share_events') == 1))))
    daily = daily.with_columns(simple.fill_null(False).alias('simple_action_known'),
        pl.when(no_event).then(1.).otherwise(pl.col('multiplier')).alias('multiplier'),
        ((pl.col('ShrOut') > 0) & pl.col('ShrOut').is_finite() &
         pl.col('date').is_between(pl.col('ShrStartDt'), pl.col('ShrEndDt')) & (pl.col('ShrAdrFlg') == 'N')).fill_null(False).alias('share_known'))
    index = calendar.select('date').with_row_index('session')
    daily = daily.join(index, on='date', how='left', validate='m:1').sort('asset', 'date')
    first = pl.col('session').shift(1).over('asset').is_null()
    consecutive = pl.col('session').diff().over('asset') == 1
    # 首次观察只是比例基准，不需要推断首次观察以前的行动；后续任何未知日永久断链。
    daily = daily.with_columns((pl.col('share_known') & (first | (consecutive & pl.col('simple_action_known')))).fill_null(False).alias('step_known'),
        pl.when(first).then(1.).otherwise(pl.col('multiplier')).alias('step_multiplier'))
    daily = daily.with_columns((~pl.col('step_known')).cast(pl.Int64).cum_sum().over('asset').alias('unknown_steps'))
    # 未知步不进入乘积。其后的输出被永久空值隔离，绝不恢复成跨越未知事件的可比股数。
    daily = daily.with_columns(pl.when(pl.col('step_known')).then(pl.col('step_multiplier').log()).otherwise(None).cum_sum().over('asset').exp().alias('cumulative_share_multiplier'))
    valid = (pl.col('unknown_steps') == 0) & pl.col('cumulative_share_multiplier').is_finite() & (pl.col('cumulative_share_multiplier') > 0)
    adjusted = pl.col('ShrOut') / pl.col('cumulative_share_multiplier')
    daily = daily.with_columns(pl.when(valid & adjusted.is_finite() & (adjusted > 0)).then(adjusted).otherwise(None).alias('event_adjusted_shares'))
    quality = dict(source_unique_rows=daily.height, unique_events=events.height,
        unknown_event_multipliers=events.filter(~pl.col('known')).height,
        valid_adjusted_observations=daily['event_adjusted_shares'].is_finite().fill_null(False).sum(),
        chain_policy='first_observation_anchor_unknown_breaks_permanently')
    return daily.select('date', 'asset', 'event_adjusted_shares'), quality


def publish_share_observation_release(config_path: Path) -> Path:
    """保留基础行情，添加股份原始观察与只供滞后读取的历史预热行。"""
    c = json.loads(config_path.read_text())
    root = Path(c['output_root']); root.mkdir(parents=True, exist_ok=False)
    write_json(root/'protocol.json', c)
    write_json(root/'registration.json', dict(at=datetime.now(timezone.utc).isoformat(), config_sha256=file_hash(config_path)))
    code = _snapshot_code(root)
    try:
        if c['version'] != 'share-observation-release-v1' or c['revision_policy'] != 'annual_historical_release_no_daily_vintage_guarantee':
            raise ValueError('股份发布版本或历史修订边界不明确')
        if c['chain_policy'] != 'first_observation_anchor_unknown_breaks_permanently' or c['share_unit'] != 'thousand_shares':
            raise ValueError('股份调整链条或单位没有冻结')
        base = Path(c['base_dataset_root'])
        names = ['market.parquet', 'state.parquet', 'calendar.parquet', 'label.parquet', 'terminal_values.parquet']
        required = {str(base/n) for n in [*names, 'manifest.json']} | {c['base_release_path'], c['source_path'], c['label_path']}
        _verify_inputs(c, required)
        manifest = json.loads((base/'manifest.json').read_text())
        for name in names:
            item = manifest['files'][name]
            if (item['sha256'] if isinstance(item, dict) else item) != c['input_sha256'][str(base/name)]:
                raise ValueError('基础清单与冻结输入不一致')
        release = FavorDataRelease.model_validate_json(Path(c['base_release_path']).read_text())
        original = pl.read_parquet(base/'market.parquet')
        state = pl.read_parquet(base/'state.parquet')
        base_calendar = pl.read_parquet(base/'calendar.parquet').select('date')
        require_panel(original, {'date', 'asset', 'open'}, '基础行情')
        require_panel(state, {'date', 'asset'}, '基础状态')
        if 'event_adjusted_shares' in original.columns:
            raise ValueError('禁止覆盖已有股份字段')
        if original['date'].max() != release.as_of_date or state['date'].max() != release.state_as_of_date:
            raise ValueError('基础发布截止日不一致')
        if release.calendar_version != file_hash(base/'calendar.parquet') or release.state_version != file_hash(base/'state.parquet'):
            raise ValueError('基础日历或状态身份不一致')
        start = date.fromisoformat(c['observation_start'])
        if start >= base_calendar['date'].min():
            raise ValueError('此发布要求显式增加基础发布之前的股份预热历史')
        mapping = c['source_columns']
        if set(mapping) != {'date', 'asset', *SHARE_FIELDS, *EVENT_FIELDS}:
            raise ValueError('股份字段映射不完整')
        raw = pl.scan_parquet(c['source_path']).select(
            pl.col(mapping['date']).alias('date'),
            (pl.lit(c['asset_prefix']) + pl.col(mapping['asset']).cast(pl.String)).alias('asset'),
            *(pl.col(mapping[f]).alias(f) for f in [*SHARE_FIELDS, *EVENT_FIELDS])).filter(pl.col('date').is_between(start, release.as_of_date)).collect()
        calendar = raw.select('date').unique().sort('date')
        if not calendar.filter(pl.col('date') >= base_calendar['date'].min()).equals(base_calendar):
            raise ValueError('来源交易日历与基础发布不一致')
        observations, quality = normalize_share_observations(raw, calendar)
        del raw
        joined = original.join(observations, on=['date', 'asset'], how='left', validate='1:1', maintain_order='left')
        if not joined.select(original.columns).equals(original):
            raise ValueError('股份连接改变了基础行情')
        warm = observations.filter(pl.col('date') < base_calendar['date'].min())
        warm_market = warm.with_columns([pl.lit(None, dtype=dtype).alias(name) for name, dtype in original.schema.items() if name not in warm.columns]).select(joined.columns)
        market = pl.concat([warm_market, joined]).sort('date', 'asset')
        warm_state = warm.select('date', 'asset').with_columns([
            pl.lit(False if dtype == pl.Boolean else None, dtype=dtype).alias(name)
            for name, dtype in state.schema.items() if name not in {'date', 'asset'}]).select(state.columns)
        extended_state = pl.concat([warm_state, state]).sort('date', 'asset')
        dataset = root/'dataset'; dataset.mkdir()
        market.write_parquet(dataset/'market.parquet')
        extended_state.write_parquet(dataset/'state.parquet')
        calendar.write_parquet(dataset/'calendar.parquet')
        for source, name in [(Path(c['label_path']), 'label.parquet'), (base/'terminal_values.parquet', 'terminal_values.parquet')]:
            reference_panel(source, dataset/name)
            if file_hash(source) != file_hash(dataset/name):
                raise ValueError('标签或终值复制不一致')
        files = {p.name: dict(sha256=file_hash(p), bytes=p.stat().st_size) for p in dataset.iterdir()}
        write_json(dataset/'manifest.json', dict(release_id=c['release_id'], as_of_date=str(release.as_of_date), files=files,
            rows=market.height, schema={k: str(v) for k, v in market.schema.items()}, revision_policy=c['revision_policy'],
            observation_start=c['observation_start'], warmup_rows=warm.height, chain_policy=c['chain_policy'],
            label_input_sha256=c['input_sha256'][c['label_path']], base_manifest_sha256=file_hash(base/'manifest.json')))
        write_json(root/'release.json', release.model_copy(update=dict(release_id=c['release_id'],
            source=release.source+'；显式公司行动调整股份及独立预热观察',
            calendar_version=files['calendar.parquet']['sha256'], state_version=files['state.parquet']['sha256'])).model_dump(mode='json'))
        _verify_inputs(c, required); _verify_code(code)
        write_json(root/'verification.json', dict(**quality, original_price_parity=True, original_state_preserved=True,
            warmup_rows=warm.height, warmup_tradable=False, labels_read=False, candidates_registered=0,
            limitation='年度历史修订并非原始公布版本；未知链条不恢复，不能将股数减少直接认作回购。'))
        write_json(root/'completion.json', dict(status='completed', manifest_sha256=file_hash(dataset/'manifest.json')))
    except Exception as error:
        write_json(root/'failure.json', dict(error=str(error))); raise
    return root
