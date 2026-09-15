"""将显式公告版本转换为年报可得面板，不计算因子或读取收益。"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date
from itertools import groupby
from pathlib import Path
import json

import polars as pl

FIELDS = ('gross_profit', 'cash_flow_from_operating_activities', 'total_assets', 'total_liabilities')
RAW_FIELDS = tuple('annual_' + x for x in FIELDS) + ('prior_annual_gross_profit', 'prior_annual_total_assets')


def annual_snapshots(versions: pl.DataFrame, calendar: pl.DataFrame, cutoff: date) -> pl.DataFrame:
    """旧年修订仅更新相应年度；同日版本冲突硬失败，下一交易日才可见。"""
    days = calendar['date'].to_list()
    if not days or days != sorted(set(days)) or calendar.schema['date'] != pl.Date:
        raise ValueError('财报对齐要求完整且有序的唯一交易日历')
    annual = versions.filter(pl.col('quarter').str.ends_with('q4')).select(
        pl.col('order_book_id').alias('asset'), pl.col('quarter').str.slice(0, 4).cast(pl.Int32).alias('year'),
        pl.col('info_date').cast(pl.Date), *FIELDS).with_columns(
        pl.date(pl.col('year'), 12, 31).alias('period_end'))
    if annual.select(pl.any_horizontal(pl.col('asset').is_null(), pl.col('info_date').is_null(),
            pl.col('info_date') < pl.col('period_end'), pl.col('info_date') > cutoff,
            *[pl.col(f).is_infinite() for f in FIELDS]).any()).item():
        raise ValueError('财报主键、公告时点或数值不合法')
    keys = ['asset', 'year', 'info_date']
    conflicts = annual.group_by(keys).agg(*[pl.col(f).drop_nulls().n_unique().alias(f) for f in FIELDS])
    if conflicts.filter(pl.any_horizontal(*[pl.col(f) > 1 for f in FIELDS])).height:
        raise ValueError('相同年度和公告日存在冲突版本，不能擅自选择')
    annual = annual.group_by(keys).agg(*[pl.col(f).drop_nulls().first() for f in FIELDS]).sort('asset', 'info_date', 'year')
    rows = []
    for asset, records in groupby(annual.iter_rows(named=True), key=lambda r: r['asset']):
        known = {}
        for announcement, same_day in groupby(records, key=lambda r: r['info_date']):
            for r in same_day:
                known[r['year']] = r
            index = bisect_right(days, announcement)
            if index == len(days) or days[index] > cutoff:
                continue
            year = max(known)
            current, previous = known[year], known.get(year - 1)
            row = dict(asset=asset, available_date=days[index], source_info_date=current['info_date'],
                       report_period_end=date(year, 12, 31), prior_info_date=previous['info_date'] if previous else None)
            row.update({'annual_' + f: current[f] for f in FIELDS})
            row.update(prior_annual_gross_profit=previous['gross_profit'] if previous else None,
                       prior_annual_total_assets=previous['total_assets'] if previous else None)
            rows.append(row)
    if not rows:
        raise ValueError('没有可用年报版本')
    # 多个非交易日公告可能在同一交易日可见；按实际公告顺序保留最后状态。
    return pl.DataFrame(rows, schema_overrides={f: pl.Float64 for f in RAW_FIELDS} | {'prior_info_date': pl.Date}).unique(
        ['asset', 'available_date'], keep='last', maintain_order=True).sort('asset', 'available_date')


def align_annual_panel(keys: pl.DataFrame, snapshots: pl.DataFrame, max_age_days: int) -> pl.DataFrame:
    """缺报与过期保持空值；分母无效时不构造可用的比率输入。"""
    if max_age_days < 366 or keys.select(pl.struct('date', 'asset').is_duplicated().any()).item():
        raise ValueError('年报期限或行情主键不合法')
    panel = keys.sort('asset', 'date').join_asof(snapshots.sort('asset', 'available_date'),
        left_on='date', right_on='available_date', by='asset', strategy='backward', check_sortedness=False)
    fresh = ((pl.col('date') - pl.col('report_period_end')).dt.total_days() <= max_age_days).fill_null(False)
    panel = panel.with_columns(*[pl.when(fresh).then(pl.col(f)).otherwise(None).alias(f) for f in RAW_FIELDS])
    return panel.with_columns(*[pl.when(pl.col(f).is_finite() & (pl.col(f) > 0)).then(pl.col(f)).otherwise(None).alias(f)
        for f in ('annual_total_assets', 'prior_annual_total_assets')])


def prepare_annual_financial_panel(config_path: Path) -> Path:
    """冻结输入身份后发布原始年报字段和可得日，失败也保留回执。"""
    from factor_miner.artifact_storage import digest_file, snapshot_code
    from factor_miner.research_report import write_json
    c = json.loads(config_path.read_text())
    root = Path(c['output_root'])
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / 'protocol.json', c)
    try:
        if c['version'] != 'annual-financial-pit-v1' or c['availability'] != 'next_session_after_announcement':
            raise ValueError('年报可得协议不符')
        paths = [*c['version_paths'], c['calendar_path'], c['market_keys_path'], c['source_manifest_path']]
        if set(paths) - set(c['input_sha256']):
            raise ValueError('年报来源未完整冻结')
        def verify():
            for p, expected in c['input_sha256'].items():
                if digest_file(Path(p)) != expected:
                    raise ValueError(f'年报输入身份变化：{p}')
        verify()
        snapshot_code(root)
        versions = pl.concat([pl.read_parquet(p).select('order_book_id', 'quarter', 'info_date', *FIELDS) for p in c['version_paths']])
        snapshots = annual_snapshots(versions, pl.read_parquet(c['calendar_path']), date.fromisoformat(c['as_of_date']))
        panel = align_annual_panel(pl.read_parquet(c['market_keys_path'], columns=['date', 'asset']), snapshots, c['max_age_days'])
        snapshots.write_parquet(root / 'annual_snapshots.parquet')
        panel.write_parquet(root / 'annual_panel.parquet')
        verify()
        write_json(root / 'manifest.json', dict(status='completed', rows=panel.height, snapshots=snapshots.height,
            source_boundary='供应商公告历史重建，未声称本地当时归档；不回填晚修订', return_labels_used=False,
            files={p.name: dict(sha256=digest_file(p), bytes=p.stat().st_size) for p in root.glob('*.parquet')}))
    except Exception as error:
        write_json(root / 'failure.json', dict(error=str(error)))
        raise
    return root
