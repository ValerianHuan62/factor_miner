"""独立收盘报价发布与既有订单成本量级审计，不推断下一开盘成交价。"""
from datetime import datetime, timezone
from pathlib import Path
import json
import math
from factor_miner.artifact_storage import reference_panel

import polars as pl

from factor_miner.favor_schema import FavorDataRelease
from factor_miner.favor_validation import require_panel
from factor_miner.research_report import write_json
from factor_miner.ridge_strategy import file_hash


QUOTE_COLUMNS = ('quote_bid_raw', 'quote_ask_raw', 'quote_close_raw')


def normalize_quotes(frame: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """同键完全相同的报价可合并；冲突硬失败，异常报价保留空值及状态。"""
    if {'date', 'asset', *QUOTE_COLUMNS} - set(frame.columns):
        raise ValueError('报价字段缺失')
    if frame.schema['date'] != pl.Date or frame.select(pl.any_horizontal(pl.col('date').is_null(), pl.col('asset').is_null()).any()).item():
        raise ValueError('报价主键类型或空值异常')
    if any(not frame.schema[c].is_numeric() for c in QUOTE_COLUMNS):
        raise ValueError('报价必须为数值类型')
    duplicates = frame.group_by('date', 'asset').agg(pl.len().alias('count'), *(pl.col(c).n_unique().alias(c) for c in QUOTE_COLUMNS)).filter(pl.col('count') > 1)
    if duplicates.filter(pl.any_horizontal(*(pl.col(c) > 1 for c in QUOTE_COLUMNS))).height:
        raise ValueError('同一证券日期有冲突报价，不能任取一行')
    frame = frame.unique(subset=['date', 'asset'], maintain_order=True)
    valid = (pl.col('quote_bid_raw').is_finite() & pl.col('quote_ask_raw').is_finite()
             & (pl.col('quote_bid_raw') > 0) & (pl.col('quote_ask_raw') >= pl.col('quote_bid_raw'))).fill_null(False)
    frame = frame.with_columns(valid.alias('valid_closing_quote'))
    frame = frame.with_columns(
        *(pl.when(valid).then(pl.col(c)).otherwise(None).cast(pl.Float64).alias(c) for c in QUOTE_COLUMNS[:2]),
        pl.when(pl.col('quote_close_raw').is_finite() & (pl.col('quote_close_raw') > 0)).then(pl.col('quote_close_raw')).otherwise(None).cast(pl.Float64).alias('quote_close_raw'))
    frame = frame.with_columns((pl.col('valid_closing_quote') & pl.col('quote_close_raw').is_not_null()
        & (pl.col('quote_ask_raw') > pl.col('quote_bid_raw'))).alias('valid_closing_quote_position'))
    return frame, dict(identical_duplicate_keys=duplicates.height, unique_keys=frame.height,
                       invalid_quote_keys=frame.filter(~pl.col('valid_closing_quote')).height)


def _verify_inputs(config: dict, required: set[str]) -> None:
    if required - set(config['input_sha256']):
        raise ValueError('依赖未完整绑定哈希')
    for name, digest in config['input_sha256'].items():
        if file_hash(Path(name)) != digest:
            raise ValueError('冻结输入已改变：' + name)


def _snapshot_code(root: Path) -> dict:
    """计算开始前保存实际包代码；结束时核对未在运行期间改变。"""
    from factor_miner.artifact_storage import snapshot_code
    return snapshot_code(root)


def _verify_code(identity: dict) -> None:
    if any(file_hash(Path(__file__).parent/name) != digest for name,digest in identity.items()):
        raise ValueError('运行期间研究代码发生变化')


def publish_quote_release(config_path: Path) -> Path:
    """显式字段映射适配只读来源；保留标准面板股票池、价格、状态和标签。"""
    c = json.loads(config_path.read_text()); root = Path(c['output_root'])
    root.mkdir(parents=True, exist_ok=False)
    write_json(root/'protocol.json', c)
    write_json(root/'registration.json', dict(at=datetime.now(timezone.utc).isoformat(), config_sha256=file_hash(config_path)))
    code = _snapshot_code(root)
    try:
        base = Path(c['base_dataset_root']); names = ['market.parquet', 'state.parquet', 'calendar.parquet', 'label.parquet', 'terminal_values.parquet']
        required = {str(base/n) for n in [*names, 'manifest.json']} | {c['source_path'], c['base_release_path']}
        _verify_inputs(c, required)
        manifest = json.loads((base/'manifest.json').read_text())
        for name in names:
            declared = manifest['files'][name]
            digest = declared['sha256'] if isinstance(declared, dict) else declared
            if digest != c['input_sha256'][str(base/name)]:
                raise ValueError('基础清单与输入文件身份不一致')
        if c['quote_price_basis'] != 'unadjusted_same_day_usd' or c['availability'] != 'after_close_t':
            raise ValueError('报价单位或可得时点未明确')
        original = pl.read_parquet(base/'market.parquet'); require_panel(original, {'date', 'asset', 'open'}, '基础行情')
        if set(QUOTE_COLUMNS) & set(original.columns):
            raise ValueError('基础面板已包含报价字段，禁止覆盖')
        mapping = c['source_columns']
        if set(mapping) != {'date', 'asset', *QUOTE_COLUMNS}:
            raise ValueError('报价字段映射必须完整且精确')
        raw = pl.scan_parquet(c['source_path']).select(
            pl.col(mapping['date']).alias('date'), (pl.lit(c['asset_prefix']) + pl.col(mapping['asset']).cast(pl.String)).alias('asset'),
            *(pl.col(mapping[n]).alias(n) for n in QUOTE_COLUMNS))
        quotes, quality = normalize_quotes(raw.filter(pl.col('date').is_between(original['date'].min(), original['date'].max())).collect())
        joined = original.join(quotes, on=['date', 'asset'], how='left', validate='1:1', maintain_order='left')
        # 未匹配的报价是显式不可用状态，不猜测、前填或改变原计算资格。
        joined = joined.with_columns(pl.col('valid_closing_quote', 'valid_closing_quote_position').fill_null(False))
        if not joined.select(original.columns).equals(original):
            raise ValueError('报价连接改变了原始行情')
        release = FavorDataRelease.model_validate_json(Path(c['base_release_path']).read_text())
        if original['date'].max() != release.as_of_date:
            raise ValueError('基础发布截止日不一致')
        if release.calendar_version != file_hash(base/'calendar.parquet') or release.state_version != file_hash(base/'state.parquet'):
            raise ValueError('基础发布日历或状态身份不一致')
        dataset = root/'dataset'; dataset.mkdir(); joined.write_parquet(dataset/'market.parquet')
        for name in names[1:]:
            reference_panel(base/name, dataset/name)
            if file_hash(base/name) != file_hash(dataset/name):
                raise ValueError('标准面板复制不一致')
        files = {p.name: dict(sha256=file_hash(p), bytes=p.stat().st_size) for p in dataset.iterdir()}
        write_json(dataset/'manifest.json', dict(release_id=c['release_id'], as_of_date=str(release.as_of_date),
            base_manifest_sha256=file_hash(base/'manifest.json'), files=files, rows=joined.height,
            schema={k:str(v) for k,v in joined.schema.items()}, quote_price_basis=c['quote_price_basis'],
            availability=c['availability'], source_protocol_sha256=file_hash(root/'protocol.json'),
            revision_policy=c['revision_policy'], labels_unchanged=True, original_prices_and_masks_unchanged=True))
        new_release = release.model_copy(update={'release_id':c['release_id'], 'source':release.source + '；独立同日未复权收盘报价'})
        write_json(root/'release.json', new_release.model_dump(mode='json'))
        _verify_inputs(c, required)
        _verify_code(code)
        write_json(root/'verification.json', dict(**quality, rows=joined.height, original_price_parity=True,
            other_panels_exact_copy=True, input_hashes_verified=True, code_identity_sha256=file_hash(root/'code_identity.json'),
            matched_quote_rows=joined['valid_closing_quote'].sum(), labels_read=False, candidates_registered=0))
        write_json(root/'completion.json', dict(status='completed', manifest_sha256=file_hash(dataset/'manifest.json')))
    except Exception as error:
        write_json(root/'failure.json', dict(error=str(error))); raise
    return root


def attach_prior_quotes(orders: pl.DataFrame, quotes: pl.DataFrame, calendar: pl.DataFrame) -> pl.DataFrame:
    """只连接成交日前一个市场交易日的收盘报价，不跨缺口补报价。"""
    if calendar.schema.get('date') != pl.Date:
        raise ValueError('日历日期类型错误')
    days = calendar['date'].to_list()
    if not days or days != sorted(set(days)):
        raise ValueError('日历重复、空或乱序')
    require_panel(quotes, {'date', 'asset', *QUOTE_COLUMNS, 'valid_closing_quote'}, '报价')
    valid_values = (pl.col('quote_bid_raw').is_finite() & pl.col('quote_ask_raw').is_finite()
                    & (pl.col('quote_bid_raw')>0) & (pl.col('quote_ask_raw')>=pl.col('quote_bid_raw'))).fill_null(False)
    if quotes.filter(pl.col('valid_closing_quote') & ~valid_values).height:
        raise ValueError('有效报价标记与价格不一致')
    if set(orders['actual_date'].to_list()) - set(days):
        raise ValueError('订单成交日不在冻结日历')
    previous = pl.DataFrame({'actual_date':days[1:], 'quote_date':days[:-1]})
    result = orders.join(previous, on='actual_date', how='left', validate='m:1').join(
        quotes.select(pl.col('date').alias('quote_date'), pl.col('asset').alias('security_id'), *QUOTE_COLUMNS, 'valid_closing_quote'),
        on=['quote_date', 'security_id'], how='left', validate='m:1')
    valid = pl.col('valid_closing_quote').fill_null(False)
    return result.with_columns(pl.when(valid).then(
        (pl.col('quote_ask_raw')-pl.col('quote_bid_raw'))/(pl.col('quote_ask_raw')+pl.col('quote_bid_raw'))*10000
    ).otherwise(None).alias('prior_quote_half_spread_bps'))


def audit_quote_costs(config_path: Path) -> Path:
    """固定订单上的报价宽度审计；不扣算伪造净值，不修改订单或回测。"""
    c = json.loads(config_path.read_text()); root = Path(c['output_root']); root.mkdir(parents=True, exist_ok=False)
    write_json(root/'protocol.json', c)
    write_json(root/'registration.json', dict(at=datetime.now(timezone.utc).isoformat(), config_sha256=file_hash(config_path)))
    code = _snapshot_code(root)
    try:
        if not math.isfinite(c['one_way_cost_bps']) or c['one_way_cost_bps'] < 0:
            raise ValueError('固定单边成本必须有限且非负')
        required = {c['market_path'], c['calendar_path'], *c['orders'].values()}
        _verify_inputs(c, required)
        quotes = pl.read_parquet(c['market_path'], columns=['date','asset',*QUOTE_COLUMNS,'valid_closing_quote'])
        calendar = pl.read_parquet(c['calendar_path']); rows = []
        for model, path in c['orders'].items():
            orders = pl.read_parquet(path).filter((pl.col('status')=='filled') & pl.col('side').is_in(['buy','sell']))
            if orders.is_empty() or orders.filter(~pl.col('gross_notional').is_finite() | (pl.col('gross_notional')<=0)).height:
                raise ValueError('成交名义金额为空或无效')
            joined = attach_prior_quotes(orders, quotes, calendar); joined.write_parquet(root/f'{model}_orders.parquet')
            for year in sorted(joined['actual_date'].dt.year().unique().to_list()):
                group = joined.filter(pl.col('actual_date').dt.year()==year); known = group.filter(pl.col('prior_quote_half_spread_bps').is_finite())
                total = group['gross_notional'].sum(); covered = known['gross_notional'].sum()
                rows.append(dict(model=model, year=year, filled_orders=group.height, quoted_orders=known.height,
                    notional_coverage=covered/total, missing_quote_notional=total-covered,
                    notional_weighted_half_spread_bps=(known['gross_notional']*known['prior_quote_half_spread_bps']).sum()/covered if covered>0 else None,
                    notional_fraction_above_fixed_cost=known.filter(pl.col('prior_quote_half_spread_bps')>c['one_way_cost_bps'])['gross_notional'].sum()/covered if covered>0 else None,
                    scope='历史成交日前一交易日收盘报价宽度；不是实际开盘价差或实际额外成本'))
        write_json(root/'summary.json', dict(rows=rows, fixed_one_way_cost_bps=c['one_way_cost_bps'], return_path_recomputed=False,
            uncertainty='开盘集合竞价、报价不同步、冲击和有效价差未知，不能把该宽度直接从旧收益扣掉。'))
        _verify_inputs(c, required); _verify_code(code)
        write_json(root/'completion.json', dict(status='completed', summary_sha256=file_hash(root/'summary.json'), code_identity_sha256=file_hash(root/'code_identity.json')))
    except Exception as error:
        write_json(root/'failure.json', dict(error=str(error))); raise
    return root
