"""普通收入分配的历史观察原型；与公告、到账现金及预测标签分开。"""
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import shutil

import polars as pl

from factor_miner.favor_validation import require_panel
from factor_miner.research_report import write_json
from factor_miner.ridge_strategy import file_hash


SOURCE_FIELDS = ('ordinary_amount', 'previous_price', 'previous_date', 'vendor_income_return')


def build_cash_observations(keys: pl.DataFrame, source: pl.DataFrame, calendar: pl.DataFrame,
                            tolerance: float = 1e-6) -> tuple[pl.DataFrame, dict]:
    """匹配固定前一市场日并验证前一期股份单位，保留合法零值和未知状态。"""
    require_panel(keys, {'date','asset'}, '基础主键')
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError('单位核验容差必须为正有限值')
    if {'date','asset',*SOURCE_FIELDS} - set(source.columns):
        raise ValueError('现金观察源字段不完整')
    if source.schema['date'] != pl.Date or source.schema['previous_date'] != pl.Date:
        raise ValueError('现金观察日期必须为Date')
    if source.select(pl.any_horizontal(pl.col('date').is_null(),pl.col('asset').is_null()).any()).item():
        raise ValueError('现金观察主键为空')
    if calendar.schema.get('date') != pl.Date:
        raise ValueError('市场日历日期类型错误')
    days = calendar['date'].to_list()
    if not days or any(d is None for d in days) or days != sorted(set(days)) or set(keys['date'])-set(days):
        raise ValueError('市场日历空、重复、乱序或缺少行情日')
    # 只检查目标发布中的证券日，不能借来源的未来存续情况改变股票池。
    source = source.join(keys.select('date','asset'), on=['date','asset'], how='semi')
    repeated = source.group_by('date','asset').agg(pl.len().alias('rows'),*(pl.col(c).n_unique().alias(c) for c in SOURCE_FIELDS)).filter(pl.col('rows')>1)
    if repeated.filter(pl.any_horizontal(*(pl.col(c)>1 for c in SOURCE_FIELDS))).height:
        raise ValueError('同一证券日有冲突现金观察')
    source = source.unique(subset=['date','asset'],maintain_order=True)
    if source.filter(pl.col('previous_date')>=pl.col('date')).height:
        raise ValueError('来源前期价格日期指向当前或未来')
    previous = pl.DataFrame({'date':days[1:], 'calendar_previous_date':days[:-1]}, schema={'date':pl.Date,'calendar_previous_date':pl.Date})
    frame = keys.select('date','asset').join(source.with_columns(pl.lit(True).alias('_source_present')),
        on=['date','asset'],how='left',validate='1:1',maintain_order='left').join(previous,on='date',how='left',validate='m:1',maintain_order='left')
    valid_amount = (pl.col('ordinary_amount').is_finite() & (pl.col('ordinary_amount')>=0)).fill_null(False)
    valid_price = (pl.col('previous_price').is_finite() & (pl.col('previous_price')>0)).fill_null(False)
    income_known = pl.col('vendor_income_return').is_finite().fill_null(False)
    consecutive = (pl.col('previous_date')==pl.col('calendar_previous_date')).fill_null(False)
    valid = pl.col('_source_present').fill_null(False) & valid_amount & valid_price & income_known & consecutive
    frame = frame.with_columns(pl.when(valid).then(pl.col('ordinary_amount')/pl.col('previous_price')).otherwise(None).alias('ordinary_cash_income_fraction'))
    comparable = frame.filter(valid)
    error = (pl.col('ordinary_cash_income_fraction')-pl.col('vendor_income_return')).abs()
    if comparable.filter(error>tolerance).height:
        raise ValueError('普通分配股份单位与供应商当期收入收益不一致')
    reason = (pl.when(pl.col('_source_present').is_null()).then(pl.lit('source_missing'))
        .when(~valid_amount).then(pl.lit('invalid_amount'))
        .when(~valid_price).then(pl.lit('invalid_previous_price'))
        .when(~consecutive).then(pl.lit('nonconsecutive_price_period'))
        .when(~income_known).then(pl.lit('income_check_unavailable')).otherwise(pl.lit('')))
    frame = frame.with_columns(valid.alias('valid_cash_observation'),reason.alias('cash_unknown_reason'),
        pl.when(~valid).then(pl.lit('unknown')).when(pl.col('ordinary_amount')>0).then(pl.lit('positive')).otherwise(pl.lit('zero')).alias('cash_observation_status'))
    output = frame.select('date','asset','ordinary_cash_income_fraction','valid_cash_observation','cash_observation_status','cash_unknown_reason',
        pl.col('previous_date').alias('cash_previous_price_date'))
    if not output.select('date','asset').equals(keys.select('date','asset')):
        raise ValueError('现金观察改变了原股票池或顺序')
    report = dict(rows=output.height,identical_duplicate_keys=repeated.height,states=output.group_by('cash_observation_status').len().sort('cash_observation_status').to_dicts(),
        unknown_reasons=output.filter(~pl.col('valid_cash_observation')).group_by('cash_unknown_reason').len().sort('cash_unknown_reason').to_dicts(),
        comparable_rows=comparable.height,max_unit_identity_error=comparable.select(error.max()).item() if comparable.height else None,
        forward_labels_used=False,stock_universe_unchanged=True)
    return output, report


def build_cash_observation_prototype(config_path: Path) -> Path:
    """正式CLI只生成独立观察表；不会登记因子或改写行情、状态、标签。"""
    c=json.loads(config_path.read_text()); root=Path(c['output_root']);root.mkdir(parents=True,exist_ok=False)
    write_json(root/'protocol.json',c)
    write_json(root/'registration.json',dict(at=datetime.now(timezone.utc).isoformat(),config_sha256=file_hash(config_path)))
    code=root/'code';code.mkdir();identity={}
    for p in Path(__file__).parent.glob('*.py'):
        shutil.copyfile(p,code/p.name);identity[p.name]=file_hash(p)
    write_json(root/'code_identity.json',identity)
    try:
        required={c['source_path'],c['market_path'],c['calendar_path'],c['state_path'],c['base_manifest_path']}
        if required-set(c['input_sha256']):raise ValueError('观察原型依赖未完整绑定')
        def verify():
            for p,h in c['input_sha256'].items():
                if file_hash(Path(p))!=h:raise ValueError('观察原型输入已改变：'+p)
        verify()
        manifest=json.loads(Path(c['base_manifest_path']).read_text())
        for field,name in [('market_path','market.parquet'),('calendar_path','calendar.parquet'),('state_path','state.parquet')]:
            declared=manifest['files'][name];digest=declared['sha256'] if isinstance(declared,dict) else declared
            if digest!=c['input_sha256'][c[field]]:raise ValueError('基础清单与原型输入不一致')
        if c['availability']!='after_close_t' or c['amount_basis']!='previous_price_period_share_basis':
            raise ValueError('现金观察时点或股份单位没有明确')
        mapping=c['source_columns']
        if set(mapping)!={'date','asset',*SOURCE_FIELDS}:raise ValueError('现金源映射必须完整且精确')
        keys=pl.read_parquet(c['market_path'],columns=['date','asset']);calendar=pl.read_parquet(c['calendar_path'],columns=['date'])
        if str(keys['date'].max())!=manifest['as_of_date']:raise ValueError('基础截止日与观察面板不一致')
        raw=pl.scan_parquet(c['source_path']).select(pl.col(mapping['date']).alias('date'),
            (pl.lit(c['asset_prefix'])+pl.col(mapping['asset']).cast(pl.String)).alias('asset'),
            *(pl.col(mapping[n]).alias(n) for n in SOURCE_FIELDS)).filter(pl.col('date').is_between(keys['date'].min(),keys['date'].max())).collect()
        output,report=build_cash_observations(keys,raw,calendar,c['unit_tolerance'])
        output.write_parquet(root/'cash_observations.parquet')
        state=pl.read_parquet(c['state_path'],columns=['date','asset','valid_for_factor_rank']);require_panel(state,{'date','asset','valid_for_factor_rank'},'排名状态')
        if state.schema['valid_for_factor_rank']!=pl.Boolean or state['valid_for_factor_rank'].null_count():raise ValueError('排名状态类型或空值异常')
        if keys.join(state.select('date','asset'),on=['date','asset'],how='anti').height:raise ValueError('基础行情缺少排名状态')
        ranked=state.filter(pl.col('valid_for_factor_rank')).join(output,on=['date','asset'],how='left',validate='1:1')
        if ranked['valid_cash_observation'].null_count():raise ValueError('排名状态存在没有原始行情的日期')
        yearly=ranked.group_by(pl.col('date').dt.year().alias('year')).agg(pl.len().alias('rankable_rows'),pl.col('valid_cash_observation').sum().alias('known_rows'),(pl.col('cash_observation_status')=='positive').sum().alias('positive_rows')).sort('year')
        report.update(yearly=yearly.to_dicts(),availability=c['availability'],amount_basis=c['amount_basis'],revision_policy=c['revision_policy'],prototype_only=True,candidates_registered=0,source_as_of=str(keys['date'].max()))
        write_json(root/'summary.json',report);verify()
        if any(file_hash(Path(__file__).parent/n)!=h for n,h in identity.items()):raise ValueError('观察原型运行期间代码变化')
        write_json(root/'completion.json',dict(status='completed',summary_sha256=file_hash(root/'summary.json'),observations_sha256=file_hash(root/'cash_observations.parquet')))
    except Exception as error:
        write_json(root/'failure.json',dict(error=str(error)));raise
    return root
