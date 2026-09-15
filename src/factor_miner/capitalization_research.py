"""历史每日市值与同单位成交输入发布；保留年度修订边界与原股票池。"""
from pathlib import Path
from datetime import datetime, timezone
import json
from factor_miner.artifact_storage import reference_panel
import polars as pl
from factor_miner.favor_schema import FavorDataRelease
from factor_miner.favor_validation import require_panel
from factor_miner.quote_research import _snapshot_code, _verify_code, _verify_inputs
from factor_miner.research_report import write_json
from factor_miner.ridge_strategy import file_hash

CAPITALIZATION_FIELDS = ('market_cap_usd', 'capitalization_price_raw', 'capitalization_volume_raw')


def normalize_capitalization(frame: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """金额已在适配入口转为美元；同日三字段不跨期填充，不使用调整后成交量。"""
    needed = {'date', 'asset', *CAPITALIZATION_FIELDS}
    if needed-set(frame.columns) or frame.schema.get('date') != pl.Date:
        raise ValueError('每日市值字段或日期类型错误')
    if frame.select(pl.any_horizontal(pl.col('date').is_null(), pl.col('asset').is_null()).any()).item():
        raise ValueError('每日市值主键为空')
    if any(not frame.schema[c].is_numeric() for c in CAPITALIZATION_FIELDS):
        raise ValueError('每日市值及交易字段必须是数值')
    duplicates=frame.group_by('date','asset').agg(pl.len().alias('n'),*(pl.col(c).n_unique().alias(c) for c in CAPITALIZATION_FIELDS)).filter(pl.col('n')>1)
    if duplicates.filter(pl.any_horizontal(*(pl.col(c)>1 for c in CAPITALIZATION_FIELDS))).height:
        raise ValueError('同一证券日期的市值或交易字段冲突')
    frame=frame.unique(subset=['date','asset'],maintain_order=True)
    valid=((pl.col('market_cap_usd')>0)&pl.col('market_cap_usd').is_finite()&
           (pl.col('capitalization_price_raw')>0)&pl.col('capitalization_price_raw').is_finite()&
           (pl.col('capitalization_volume_raw')>=0)&pl.col('capitalization_volume_raw').is_finite()).fill_null(False)
    frame=frame.with_columns(valid.alias('valid_capitalization_observation'))
    frame=frame.with_columns(*(pl.when(valid).then(pl.col(c)).otherwise(None).cast(pl.Float64).alias(c) for c in CAPITALIZATION_FIELDS))
    return frame,dict(identical_duplicate_keys=duplicates.height,source_unique_keys=frame.height,invalid_keys=frame.filter(~pl.col("valid_capitalization_observation")).height)



def normalize_return_context(raw: pl.DataFrame, calendar: pl.DataFrame) -> pl.DataFrame:
    """股票收益只接受前一固定市场日；市场指数同日各证券记录必须一致。"""
    fields=('reported_price_return','previous_price_date','leader_return')
    if calendar.is_empty() or calendar.schema.get('date')!=pl.Date or calendar['date'].null_count():
        raise ValueError('市场日历为空或日期类型错误')
    if calendar['date'].n_unique()!=calendar.height or calendar['date'].to_list()!=sorted(calendar['date'].to_list()):
        raise ValueError('市场日历必须唯一有序')
    if raw.schema.get('date')!=pl.Date or raw.schema.get('previous_price_date')!=pl.Date:
        raise ValueError('收益观察日期类型错误')
    if raw.filter(pl.col('previous_price_date')>=pl.col('date')).height:raise ValueError('收益前期日期指向当前或未来')
    dup=raw.group_by('date','asset').agg(*(pl.col(n).n_unique().alias(n) for n in fields))
    if dup.filter(pl.any_horizontal(*(pl.col(n)>1 for n in fields))).height:raise ValueError('同日收益上下文冲突')
    raw=raw.unique(subset=['date','asset'],maintain_order=True)
    index=raw.group_by('date').agg(pl.col('leader_return').n_unique().alias('n'))
    if index.filter(pl.col('n')!=1).height:raise ValueError('同日市场指数记录不一致')
    days=calendar['date'].to_list()
    prior=pl.DataFrame({'date':days[1:],'fixed_previous_date':days[:-1]})
    raw=raw.join(prior,on='date',how='left',validate='m:1',maintain_order='left')
    valid=((pl.col('previous_price_date')==pl.col('fixed_previous_date'))&pl.col('reported_price_return').is_finite()&(pl.col('reported_price_return')>=-1)).fill_null(False)
    if raw.filter(~pl.col('leader_return').is_finite().fill_null(False)).height:raise ValueError('市场指数收益不可用')
    return raw.select('date','asset',pl.when(valid).then(pl.col('reported_price_return')).otherwise(None).cast(pl.Float64).alias('stock_price_return'),pl.col('leader_return').cast(pl.Float64))


def publish_capitalization_release(config_path: Path) -> Path:
    """显式来源的每日历史市值进入独立发布；不假装具有原始逐日公告版本。"""
    c=json.loads(config_path.read_text());root=Path(c['output_root']);root.mkdir(parents=True,exist_ok=False)
    write_json(root/'protocol.json',c);write_json(root/'registration.json',dict(at=datetime.now(timezone.utc).isoformat(),config_sha256=file_hash(config_path)))
    code=_snapshot_code(root)
    try:
        base=Path(c['base_dataset_root']);names=['market.parquet','state.parquet','calendar.parquet','label.parquet','terminal_values.parquet']
        required={str(base/n) for n in [*names,'manifest.json']}|{c['source_path'],c['base_release_path']}
        _verify_inputs(c,required)
        if c['availability']!='after_close_t' or c['capitalization_unit']!='thousand_usd' or c['price_volume_basis']!='unadjusted_same_day':
            raise ValueError('市值单位或观察时点没有明确')
        if c['revision_policy']!='annual_historical_release_no_daily_vintage_guarantee':
            raise ValueError('必须保留年度历史修订限制')
        manifest=json.loads((base/'manifest.json').read_text())
        for n in names:
            entry=manifest['files'][n];digest=entry['sha256'] if isinstance(entry,dict) else entry
            if digest!=c['input_sha256'][str(base/n)]:raise ValueError('基础清单与输入不一致')
        original=pl.read_parquet(base/'market.parquet');require_panel(original,{'date','asset','open'},'基础行情')
        if set(CAPITALIZATION_FIELDS)&set(original.columns):raise ValueError('禁止覆盖已有字段')
        m=c['source_columns']
        if set(m)!={'date','asset',*CAPITALIZATION_FIELDS}:raise ValueError('字段映射不完整')
        raw=pl.scan_parquet(c['source_path']).select(pl.col(m['date']).alias('date'),
            (pl.lit(c['asset_prefix'])+pl.col(m['asset']).cast(pl.String)).alias('asset'),
            (pl.col(m['market_cap_usd'])*1000).alias('market_cap_usd'),
            *(pl.col(m[n]).alias(n) for n in CAPITALIZATION_FIELDS[1:])).filter(pl.col('date').is_between(original['date'].min(),original['date'].max())).collect()
        raw=raw.join(original.select('date','asset'),on=['date','asset'],how='semi')
        normalized,quality=normalize_capitalization(raw)
        joined=original.join(normalized,on=['date','asset'],how='left',validate='1:1',maintain_order='left').with_columns(pl.col('valid_capitalization_observation').fill_null(False))
        if c.get('include_return_context',False):
            rm=c['return_columns']
            if set(rm)!={'reported_price_return','previous_price_date','leader_return'} or c['leader_definition']!='crsp_value_weighted_price_return':
                raise ValueError('收益上下文字段或指数定义不明确')
            if {'stock_price_return','leader_return'}&set(joined.columns):raise ValueError('禁止覆盖已有收益上下文')
            context=pl.scan_parquet(c['source_path']).select(pl.col(m['date']).alias('date'),
                (pl.lit(c['asset_prefix'])+pl.col(m['asset']).cast(pl.String)).alias('asset'),
                *(pl.col(rm[n]).alias(n) for n in rm)).filter(pl.col('date').is_between(original['date'].min(),original['date'].max())).collect()
            context=context.join(original.select('date','asset'),on=['date','asset'],how='semi')
            context=normalize_return_context(context,pl.read_parquet(base/'calendar.parquet',columns=['date']))
            joined=joined.join(context,on=['date','asset'],how='left',validate='1:1',maintain_order='left')
        if not joined.select(original.columns).equals(original):raise ValueError('连接改变原始行情')
        release=FavorDataRelease.model_validate_json(Path(c['base_release_path']).read_text())
        if original['date'].max()!=release.as_of_date or release.calendar_version!=file_hash(base/'calendar.parquet') or release.state_version!=file_hash(base/'state.parquet'):
            raise ValueError('截止日、日历或状态身份不一致')
        dataset=root/'dataset';dataset.mkdir();joined.write_parquet(dataset/'market.parquet')
        for n in names[1:]:
            reference_panel(base/n,dataset/n)
            if file_hash(base/n)!=file_hash(dataset/n):raise ValueError('基础面板复制不一致')
        files={p.name:dict(sha256=file_hash(p),bytes=p.stat().st_size) for p in dataset.iterdir()}
        write_json(dataset/'manifest.json',dict(release_id=c['release_id'],as_of_date=str(release.as_of_date),files=files,rows=joined.height,
            base_manifest_sha256=file_hash(base/'manifest.json'),schema={k:str(v) for k,v in joined.schema.items()},revision_policy=c['revision_policy'],labels_unchanged=True,availability=c['availability']))
        write_json(root/'release.json',release.model_copy(update={'release_id':c['release_id'],'source':release.source+'；同日原始价格与成交股数、CRSP历史每日市值'}).model_dump(mode='json'))
        state=pl.read_parquet(base/'state.parquet',columns=['date','asset','valid_for_factor_rank'])
        ranked=state.filter(pl.col('valid_for_factor_rank')).join(joined,on=['date','asset'],how='left',validate='1:1')
        if ranked['valid_capitalization_observation'].null_count():raise ValueError('排名状态缺少行情')
        yearly=ranked.group_by(pl.col('date').dt.year().alias('year')).agg(pl.len().alias('rankable_rows'),pl.col('valid_capitalization_observation').sum().alias('valid_rows')).sort('year')
        _verify_inputs(c,required);_verify_code(code)
        write_json(root/'verification.json',dict(**quality,rows=joined.height,yearly=yearly.to_dicts(),original_price_parity=True,other_panels_exact_copy=True,labels_read=False,candidates_registered=0))
        write_json(root/'completion.json',dict(status='completed',manifest_sha256=file_hash(dataset/'manifest.json'),verification_sha256=file_hash(root/'verification.json')))
    except Exception as error:
        write_json(root/'failure.json',dict(error=str(error)));raise
    return root
