"""历史行业同业收益发布，剔除自身发行人并使用前一市场日市值。"""
from datetime import datetime, timezone
import json
from pathlib import Path
from factor_miner.artifact_storage import reference_panel

import polars as pl

from factor_miner.favor_schema import FavorDataRelease
from factor_miner.favor_validation import require_panel
from factor_miner.quote_research import _snapshot_code, _verify_code, _verify_inputs
from factor_miner.research_report import write_json
from factor_miner.ridge_strategy import file_hash


def attach_classification(keys: pl.DataFrame, master: pl.DataFrame) -> pl.DataFrame:
    """有效区间精确连接，未知保留，重叠分类或发行人冲突硬失败。"""
    required = {'asset', 'valid_from', 'valid_to', 'sic_code', 'issuer_id'}
    if required - set(master.columns):
        raise ValueError('行业主表缺少分类、发行人或有效日期')
    if master.schema['valid_from'] != pl.Date or master.schema['valid_to'] != pl.Date:
        raise ValueError('行业区间必须使用日期类型')
    if master.filter(pl.col('valid_from') > pl.col('valid_to')).height:
        raise ValueError('行业有效区间倒置')
    records = master.select(sorted(required)).unique()
    matched = keys.join(records, on='asset', how='inner').filter(
        pl.col('date').is_between(pl.col('valid_from'), pl.col('valid_to'))).select('date', 'asset', 'sic_code', 'issuer_id').unique()
    if matched.select(pl.struct('date', 'asset').is_duplicated().any()).item():
        raise ValueError('行业或发行人有效区间冲突')
    return keys.join(matched, on=['date', 'asset'], how='left', validate='1:1').with_columns(
        pl.when(pl.col('sic_code').is_between(100, 9999)).then(pl.col('sic_code') // 100).otherwise(None).alias('industry_code'))


def industry_context(frame: pl.DataFrame, calendar: pl.DataFrame, *, min_peer_issuers: int,
                     min_leader_issuers: int, leader_fraction: float, min_known_weight: float) -> pl.DataFrame:
    """先按前日规模选行业大公司，再计算排除整个自身发行人的已实现同业收益。"""
    require_panel(frame, {'date', 'asset', 'issuer_id', 'industry_code', 'market_cap_usd', 'stock_price_return', 'valid_for_factor_rank'}, '行业输入')
    if min_peer_issuers < 1 or min_leader_issuers < 1 or not 0 < leader_fraction < 1 or not 0 < min_known_weight <= 1:
        raise ValueError('行业人数或权重覆盖参数非法')
    days = calendar['date'].to_list()
    if not days or calendar.schema['date'] != pl.Date or None in days or days != sorted(set(days)):
        raise ValueError('行业发布需要唯一有序的完整市场日历')
    if frame.filter(~pl.col('date').is_in(days)).height:
        raise ValueError('行情日期不在市场日历')
    if frame.schema['valid_for_factor_rank'] != pl.Boolean or frame['valid_for_factor_rank'].null_count():
        raise ValueError('行业排名资格必须为非空布尔')
    previous = pl.DataFrame({'date': days[1:], 'previous_date': days[:-1]})
    cap = frame.select(pl.col('date').alias('previous_date'), 'asset', pl.col('market_cap_usd').alias('lag_cap'))
    x = frame.join(previous, on='date', how='left', validate='m:1').join(cap, on=['previous_date', 'asset'], how='left', validate='m:1')
    eligible = x.filter(pl.col('valid_for_factor_rank') & pl.col('issuer_id').is_not_null()
                        & pl.col('industry_code').is_not_null() & pl.col('lag_cap').is_finite() & (pl.col('lag_cap') > 0))
    valid_return = pl.col('stock_price_return').is_finite() & (pl.col('stock_price_return') >= -1)
    # 缺收益不影响事前权重和大公司选择，只影响已观察收益的权重覆盖。
    eligible = eligible.with_columns(pl.when(valid_return).then(pl.col('lag_cap')).otherwise(0.).alias('known_weight'),
        pl.when(valid_return).then(pl.col('lag_cap') * pl.col('stock_price_return')).otherwise(0.).alias('weighted_return'))
    group = ['date', 'industry_code']
    firms = eligible.group_by(*group, 'issuer_id').agg(pl.col('lag_cap').sum().alias('weight'),
        pl.col('known_weight').sum(), pl.col('weighted_return').sum())
    firms = firms.with_columns((pl.col('weight').rank(method='average').over(group) / pl.len().over(group) > 1 - leader_fraction).alias('is_leader'))
    firms = firms.with_columns(*(pl.when(pl.col('is_leader')).then(pl.col(n)).otherwise(0.).alias('leader_' + n)
                                for n in ['weight', 'known_weight', 'weighted_return']))
    totals = firms.group_by(group).agg(pl.len().alias('total_firms'), pl.col('is_leader').sum().alias('total_leaders'),
        *(pl.col(n).sum().alias('total_' + n) for n in ['weight', 'known_weight', 'weighted_return', 'leader_weight', 'leader_known_weight', 'leader_weighted_return']))
    targets = frame.select('date', 'asset', 'issuer_id', 'industry_code').join(totals, on=group, how='left', validate='m:1')
    own = firms.drop('is_leader').with_columns(pl.lit(1).alias('own_firm')).join(
        firms.select(*group, 'issuer_id', pl.col('is_leader').cast(pl.Int64).alias('own_leader')), on=[*group, 'issuer_id'], validate='1:1')
    targets = targets.join(own, on=[*group, 'issuer_id'], how='left', validate='m:1')
    targets = targets.with_columns((pl.col('total_firms') - pl.col('own_firm').fill_null(0)).alias('industry_peer_count'),
        (pl.col('total_leaders') - pl.col('own_leader').fill_null(0)).alias('industry_leader_count'))
    for prefix, name in [('', 'industry_peer_return'), ('leader_', 'industry_leader_return')]:
        weight = pl.col('total_' + prefix + 'weight') - pl.col(prefix + 'weight').fill_null(0.)
        known = pl.col('total_' + prefix + 'known_weight') - pl.col(prefix + 'known_weight').fill_null(0.)
        numerator = pl.col('total_' + prefix + 'weighted_return') - pl.col(prefix + 'weighted_return').fill_null(0.)
        count = pl.col('industry_leader_count') if prefix else pl.col('industry_peer_count')
        minimum = min_leader_issuers if prefix else min_peer_issuers
        valid = pl.col('issuer_id').is_not_null() & (count >= minimum) & (weight > 0) & (known > 0) & (known / weight >= min_known_weight)
        targets = targets.with_columns(pl.when(valid).then(numerator / known).otherwise(None).alias(name))
    return targets.select('date', 'asset', 'industry_peer_return', 'industry_leader_return', 'industry_peer_count', 'industry_leader_count')


def publish_industry_release(config_path: Path) -> Path:
    """发布独立同业输入，完整保留基础价格、股票池、标签和历史修订限制。"""
    c = json.loads(config_path.read_text())
    root = Path(c['output_root'])
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / 'protocol.json', c)
    write_json(root / 'registration.json', dict(at=datetime.now(timezone.utc).isoformat(), config_sha256=file_hash(config_path)))
    code = _snapshot_code(root)
    try:
        base = Path(c['base_dataset_root'])
        names = ['market.parquet', 'state.parquet', 'calendar.parquet', 'label.parquet', 'terminal_values.parquet']
        required = {str(base / n) for n in [*names, 'manifest.json']} | {c['master_path'], c['base_release_path']}
        _verify_inputs(c, required)
        if c['availability'] != 'after_close_t' or c['revision_policy'] != 'annual_historical_release_no_daily_vintage_guarantee':
            raise ValueError('行业时点或历史修订边界不明确')
        manifest = json.loads((base / 'manifest.json').read_text())
        for name in names:
            entry = manifest['files'][name]
            if (entry['sha256'] if isinstance(entry, dict) else entry) != c['input_sha256'][str(base / name)]:
                raise ValueError('基础清单与面板身份不一致')
        original = pl.read_parquet(base / 'market.parquet')
        require_panel(original, {'date', 'asset', 'open', 'market_cap_usd', 'stock_price_return'}, '基础行情')
        new_fields = {'industry_peer_return', 'industry_leader_return', 'industry_peer_count', 'industry_leader_count', 'industry_code', 'issuer_id'}
        if new_fields & set(original.columns):
            raise ValueError('禁止覆盖已有行业字段')
        mapping = c['master_columns']
        if set(mapping) != {'asset', 'valid_from', 'valid_to', 'sic_code', 'issuer_id'}:
            raise ValueError('行业字段映射不完整')
        master = pl.read_parquet(c['master_path']).select(*(pl.col(v).alias(k) for k, v in mapping.items())).with_columns(pl.col('issuer_id').cast(pl.String))
        master = master.with_columns(pl.when(~pl.col('issuer_id').is_in(c['invalid_issuer_values'])).then(pl.col('issuer_id')).otherwise(None).alias('issuer_id'))
        pieces = []
        for year in sorted(original['date'].dt.year().unique()):
            keys = original.filter(pl.col('date').dt.year() == year).select('date', 'asset')
            classified = attach_classification(keys, master.filter((pl.col('valid_from') <= keys['date'].max()) & (pl.col('valid_to') >= keys['date'].min())))
            pieces.append(classified.select('date', 'asset', 'industry_code', 'issuer_id'))
        classification = pl.concat(pieces)
        state = pl.read_parquet(base / 'state.parquet', columns=['date', 'asset', 'valid_for_factor_rank'])
        inputs = original.select('date', 'asset', 'market_cap_usd', 'stock_price_return').join(classification, on=['date', 'asset'], validate='1:1').join(state, on=['date', 'asset'], validate='1:1')
        context = industry_context(inputs, pl.read_parquet(base / 'calendar.parquet', columns=['date']), **c['peer_policy'])
        joined = original.join(classification, on=['date', 'asset'], how='left', validate='1:1', maintain_order='left').join(context, on=['date', 'asset'], how='left', validate='1:1', maintain_order='left')
        if not joined.select(original.columns).equals(original):
            raise ValueError('行业发布改变原始面板')
        release = FavorDataRelease.model_validate_json(Path(c['base_release_path']).read_text())
        if original['date'].max() != release.as_of_date or release.calendar_version != file_hash(base / 'calendar.parquet') or release.state_version != file_hash(base / 'state.parquet'):
            raise ValueError('行业发布与基础日期、状态或日历不一致')
        dataset = root / 'dataset'
        dataset.mkdir()
        joined.write_parquet(dataset / 'market.parquet')
        for n in names[1:]:
            reference_panel(base / n, dataset / n)
            if file_hash(base / n) != file_hash(dataset / n):
                raise ValueError('基础面板复制不一致')
        ranked = joined.join(state.filter(pl.col('valid_for_factor_rank')).select('date', 'asset'), on=['date', 'asset'], how='semi')
        quality = ranked.group_by(pl.col('date').dt.year().alias('year')).agg(pl.len().alias('rankable_rows'),
            pl.col('industry_code').is_not_null().mean().alias('classification_coverage'),
            pl.col('industry_peer_return').is_finite().fill_null(False).mean().alias('peer_return_coverage'),
            pl.col('industry_leader_return').is_finite().fill_null(False).mean().alias('leader_return_coverage')).sort('year')
        write_json(dataset / 'manifest.json', dict(release_id=c['release_id'], as_of_date=str(release.as_of_date), rows=joined.height,
            files={p.name: dict(sha256=file_hash(p), bytes=p.stat().st_size) for p in dataset.iterdir()},
            schema={k: str(v) for k, v in joined.schema.items()}, base_manifest_sha256=file_hash(base / 'manifest.json'),
            labels_unchanged=True, availability=c['availability'], revision_policy=c['revision_policy'], peer_policy=c['peer_policy']))
        write_json(root / 'release.json', release.model_copy(update={'release_id': c['release_id'], 'source': release.source + '；历史行业区间与排除自身发行人的同业收益'}).model_dump(mode='json'))
        _verify_inputs(c, required)
        _verify_code(code)
        write_json(root / 'verification.json', dict(rows=joined.height, yearly=quality.to_dicts(), original_panel_unchanged=True,
            copied_panels_identical=True, labels_read=False, candidates_registered=0, self_issuer_excluded=True,
            weight_basis='previous_fixed_market_session_market_cap', classification_basis='historical_effective_interval'))
        write_json(root / 'completion.json', dict(status='completed', manifest_sha256=file_hash(dataset / 'manifest.json'), verification_sha256=file_hash(root / 'verification.json')))
    except Exception as error:
        write_json(root / 'failure.json', dict(error=str(error)))
        raise
    return root
