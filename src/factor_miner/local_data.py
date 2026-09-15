"""把用户本地 CSV 或 Parquet 转换为可审计的跨市场标准发布。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import subprocess

import polars as pl

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.data_source import (
    EXECUTION_STATE_COLUMNS,
    IDENTITY_COLUMNS,
    MASK_COLUMNS,
    STATE_BASE_COLUMNS,
)
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.label_dataset import build_fixed_session_o2o_labels


_REQUIRED_MARKET_COLUMNS = ("date", "asset", "open", "high", "low", "close", "volume")


@dataclass(frozen=True, slots=True)
class LocalDataRelease:
    """一次本地标准数据发布的入口文件。"""

    release_id: str
    release_root: Path
    manifest_path: Path
    config_path: Path


def _error(message: str) -> FactorMinerError:
    return FactorMinerError(FailureCode.DATA_RELEASE_MISMATCH, message)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_input(path: Path) -> pl.DataFrame:
    if not path.is_file():
        raise _error(f"本地数据文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pl.read_csv(path, try_parse_dates=True)
    elif suffix in {".parquet", ".pq"}:
        frame = pl.read_parquet(path)
    else:
        raise _error("本地数据只支持 CSV 或 Parquet")
    missing = set(_REQUIRED_MARKET_COLUMNS).difference(frame.columns)
    if missing:
        raise _error(f"本地行情缺少标准列：{sorted(missing)}")
    if frame.schema.get("date") == pl.Datetime:
        frame = frame.with_columns(pl.col("date").cast(pl.Date))
    elif frame.schema.get("date") != pl.Date:
        try:
            frame = frame.with_columns(pl.col("date").cast(pl.String).str.to_date(strict=True))
        except Exception as error:
            raise _error(f"date 无法解析为交易日期：{error}") from error
    return frame


def _validate_market(frame: pl.DataFrame) -> pl.DataFrame:
    selected = [*_REQUIRED_MARKET_COLUMNS]
    if "amount" in frame.columns:
        selected.append("amount")
    selected.extend(column for column in MASK_COLUMNS if column in frame.columns)
    selected.extend(column for column in EXECUTION_STATE_COLUMNS if column in frame.columns)
    selected.extend(column for column in STATE_BASE_COLUMNS if column in frame.columns)
    frame = frame.select(selected).sort(list(IDENTITY_COLUMNS))
    if frame.select(pl.any_horizontal(pl.col("date").is_null(), pl.col("asset").is_null())).to_series().any():
        raise _error("date/asset 主键不能包含空值")
    duplicates = frame.group_by(list(IDENTITY_COLUMNS)).len().filter(pl.col("len") > 1)
    if duplicates.height:
        raise _error(f"本地行情存在重复 date/asset 主键：{duplicates.head(1).to_dicts()}")
    numeric = ("open", "high", "low", "close", "volume")
    invalid = frame.select(
        pl.any_horizontal(
            *[pl.col(column).cast(pl.Float64, strict=False).is_null() for column in numeric]
        ).any()
    ).item()
    if invalid:
        raise _error("OHLCV 必须全部是可转换为数值的非空值")
    frame = frame.with_columns(pl.col(column).cast(pl.Float64) for column in numeric)
    bad_price = frame.filter(
        (pl.col("open") <= 0)
        | (pl.col("high") <= 0)
        | (pl.col("low") <= 0)
        | (pl.col("close") <= 0)
        | (pl.col("volume") < 0)
        | (pl.col("high") < pl.max_horizontal("open", "close", "low"))
        | (pl.col("low") > pl.min_horizontal("open", "close", "high"))
    )
    if bad_price.height:
        raise _error(f"OHLCV 数值关系非法：{bad_price.head(1).to_dicts()}")
    return frame


def prepare_local_release(
    input_path: Path,
    output_root: Path,
    *,
    artifact_root: Path,
    adjustment_convention: str,
    calendar_version: str,
    data_origin: str = "local_files",
    assume_tradable: bool = False,
    repository_root: Path | None = None,
) -> LocalDataRelease:
    """生成 market/state/label、manifest 和可直接传给 ``doctor`` 的配置。"""

    source = input_path.expanduser().resolve(strict=False)
    release_root = output_root.expanduser().resolve(strict=False)
    artifacts = artifact_root.expanduser().resolve(strict=False)
    if release_root == artifacts or release_root in artifacts.parents or artifacts in release_root.parents:
        raise _error("数据发布目录与运行产物目录必须分离")
    release_root.mkdir(parents=True, exist_ok=True)
    targets = tuple(release_root / name for name in ("market.parquet", "state.parquet", "label.parquet", "manifest.json", "runtime.env"))
    if any(path.exists() for path in targets):
        raise _error("输出目录已包含正式发布文件；请使用新的空目录")

    frame = _validate_market(_read_input(source))
    supplied_masks = set(MASK_COLUMNS).intersection(frame.columns)
    supplied_execution = set(EXECUTION_STATE_COLUMNS).intersection(frame.columns)
    supplied_a_share_execution = {"can_buy", "can_sell"}.intersection(frame.columns)
    if supplied_masks and supplied_masks != set(MASK_COLUMNS):
        raise _error("三类有效性 mask 必须全部提供或全部省略")
    if supplied_masks:
        if supplied_execution and supplied_execution != set(EXECUTION_STATE_COLUMNS):
            raise _error("can_open_long/can_close_long 必须同时提供")
        if supplied_a_share_execution and supplied_a_share_execution != {"can_buy", "can_sell"}:
            raise _error("can_buy/can_sell 必须同时提供")
        if not supplied_execution and not supplied_a_share_execution:
            raise _error("状态表必须显式提供 can_open_long/can_close_long，A 股可使用 can_buy/can_sell")
        if supplied_execution:
            state = frame.select(
                [*IDENTITY_COLUMNS, *MASK_COLUMNS, *EXECUTION_STATE_COLUMNS]
            )
        else:
            state = frame.select(
                [*IDENTITY_COLUMNS, *MASK_COLUMNS, "can_buy", "can_sell"]
            ).with_columns(
                pl.col("can_buy").alias("can_open_long"),
                pl.col("can_sell").alias("can_close_long"),
            ).select([*IDENTITY_COLUMNS, *MASK_COLUMNS, *EXECUTION_STATE_COLUMNS])
        if any(state.schema.get(column) != pl.Boolean for column in MASK_COLUMNS):
            raise _error("三类有效性 mask 必须是 Boolean 类型")
        if any(state.schema.get(column) != pl.Boolean for column in EXECUTION_STATE_COLUMNS):
            raise _error("can_open_long/can_close_long 必须是 Boolean 类型")
        state_version = "user-supplied-masks-v1"
    else:
        if not assume_tradable:
            raise _error("数据缺少有效性 mask；确认数据已处理停牌和可交易状态后显式使用 --assume-tradable")
        state = frame.select(list(IDENTITY_COLUMNS)).with_columns(
            pl.lit(True).alias(column) for column in MASK_COLUMNS
        ).with_columns(
            pl.lit(True).alias(column) for column in EXECUTION_STATE_COLUMNS
        )
        state_version = "explicit-assume-tradable-v1"

    market_columns = [*_REQUIRED_MARKET_COLUMNS]
    if "amount" in frame.columns:
        market_columns.append("amount")
    market = frame.select(market_columns)
    label = build_fixed_session_o2o_labels(market)
    market_path, state_path, label_path = targets[:3]
    market.write_parquet(market_path)
    state.write_parquet(state_path)
    label.write_parquet(label_path)
    file_hashes = {
        "market_sha256": _sha256_file(market_path),
        "state_sha256": _sha256_file(state_path),
        "label_sha256": _sha256_file(label_path),
    }
    release_id = f"local_{sha256_json(file_hashes)[:24]}"
    cutoff = market.get_column("date").max()
    if cutoff is None:
        raise _error("本地行情不能为空")
    manifest = {
        "release_id": release_id,
        "release_kind": "standard_panel_v2",
        "data_origin": data_origin,
        "release_root": str(release_root),
        "label_release_root": str(release_root),
        "market_uri": str(market_path),
        "state_uri": str(state_path),
        "label_uri": str(label_path),
        **file_hashes,
        "schema_version": "standard-panel-v2",
        "label_formula_version": "fixed_exchange_sessions_open_t_plus_6_over_open_t_plus_1_v2",
        "label_event_rule": "signal_t_entry_t_plus_1_exit_t_plus_6",
        "market_cutoff": cutoff.isoformat(),
        "adjustment_convention": adjustment_convention,
        "calendar_version": calendar_version,
        "state_table_version": state_version,
        "state_table_cutoff": cutoff.isoformat(),
        "source_file_sha256": _sha256_file(source),
    }
    manifest_path = targets[3]
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
    try:
        code_commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise _error("无法读取当前 Git 提交；正式配置必须绑定代码身份") from error
    config_path = targets[4]
    values = {
        "FM_MODE": "visible",
        "FM_QUANTLAKE_ROOT": "",
        "FM_ARTIFACT_ROOT": str(artifacts),
        "FM_SMOKE_INPUT_ROOT": "",
        "FM_DERIVED_RELEASE_ROOT": "",
        "FM_MARKET_URI": str(market_path),
        "FM_STATE_URI": str(state_path),
        "FM_LABEL_URI": str(label_path),
        "FM_DATA_ORIGIN": data_origin,
        "FM_RESOLVED_RELEASE_ID": release_id,
        "FM_RELEASE_MANIFEST_PATH": str(manifest_path),
        "FM_RELEASE_MANIFEST_SHA256": _sha256_file(manifest_path),
        "FM_SCHEMA_VERSION": "standard-panel-v2",
        "FM_MARKET_CUTOFF": cutoff.isoformat(),
        "FM_ADJUSTMENT_CONVENTION": adjustment_convention,
        "FM_CALENDAR_VERSION": calendar_version,
        "FM_STATE_TABLE_VERSION": state_version,
        "FM_STATE_TABLE_CUTOFF": cutoff.isoformat(),
        "FM_CODE_COMMIT": code_commit,
        "FM_CONFIG_PATH": str(config_path),
    }
    values["FM_CONFIG_HASH"] = hashlib.sha256(canonical_json_bytes(values)).hexdigest()
    config_path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    return LocalDataRelease(release_id, release_root, manifest_path, config_path)


def publish_mapped_history(config_path: Path) -> Path:
    """按显式列映射发布历史面板；复用上游状态，不猜交易资格或读取收益统计。"""
    import json
    from datetime import date, datetime, timezone
    from factor_miner.artifact_storage import snapshot_code, digest_file
    from factor_miner.favor_schema import FavorDataRelease
    from factor_miner.favor_validation import require_panel
    from factor_miner.horizon_research import tradable_fixed_labels
    from factor_miner.research_report import write_json
    c = json.loads(config_path.read_text())
    root = Path(c['output_root'])
    root.mkdir(parents=True, exist_ok=False)
    write_json(root/'protocol.json', c)
    write_json(root/'registration.json', dict(at=datetime.now(timezone.utc).isoformat(), config_sha256=digest_file(config_path)))
    try:
        horizon = c['holding_sessions']
        if (c['version'] not in {'mapped-history-v1', 'mapped-history-v2'}
                or horizon not in {1, 5, 20}
                or (c['version'] == 'mapped-history-v1' and horizon != 5)):
            raise ValueError('映射发布版本或固定期限标签合同不符')
        release = FavorDataRelease.model_validate(c['release'])
        start, end = date.fromisoformat(c['start']), date.fromisoformat(c['end'])
        if not start < end or end != release.as_of_date:
            raise ValueError('历史范围与发布截止日不符')
        sources = c['panels']
        required = {v['path'] for v in sources.values()} | {c['source_manifest_path']}
        if required - set(c['input_sha256']):
            raise ValueError('发布来源未完整冻结')
        def verify():
            for path, expected in c['input_sha256'].items():
                if digest_file(Path(path)) != expected:
                    raise ValueError(f'发布来源变化：{path}')
        verify()
        manifest = json.loads(Path(c['source_manifest_path']).read_text())
        if manifest['release_id'] != c['source_release_id']:
            raise ValueError('上游发布身份不符')
        snapshot_code(root)
        frames = {}
        for name in ('market', 'state', 'calendar', 'terminal_values'):
            entry = sources[name]
            frame = pl.scan_parquet(entry['path'])
            if 'equals' in entry:
                for key, value in entry['equals'].items():
                    frame = frame.filter(pl.col(key) == value)
            frame = frame.select(*(pl.col(source).alias(target) for target, source in entry['columns'].items()))
            date_key = 'event_date' if name == 'terminal_values' else 'date'
            frame = frame.filter(pl.col(date_key).is_between(start, end))
            if name == 'terminal_values':
                frame = frame.filter(pl.col('available_at') <= end)
            frames[name] = frame.collect()
        market, state, calendar = (frames[k] for k in ('market', 'state', 'calendar'))
        terminal_fields={'security_id','event_date','available_at','terminal_value','source'}
        if terminal_fields-set(frames['terminal_values'].columns):
            raise ValueError('终止面板必须先转换为已核实terminal_value，不能直接映射原始现金或退市收益事件')
        calendar = calendar.sort('date')
        if calendar.schema['date'] != pl.Date or calendar['date'].null_count() or calendar['date'].n_unique() != calendar.height:
            raise ValueError('日历缺失、重复或类型不符')
        masks = {'valid_for_factor_compute','valid_for_factor_rank','can_open_long','can_close_long','valid_for_o2o_label'}
        require_panel(market, {'date','asset','open','high','low','close','volume'}, '映射行情')
        require_panel(state, {'date','asset',*masks}, '映射状态')
        if any(state.schema[k] != pl.Boolean or state[k].null_count() for k in masks):
            raise ValueError('状态掩码缺失或类型错误')
        off = state.join(calendar, on='date', how='anti')
        if off.height and off.select(pl.any_horizontal(*masks).any()).item():
            raise ValueError('非交易日有可用状态，不能自动删除')
        market = market.join(calendar, on='date', how='semi').sort('asset','date')
        state = state.join(calendar, on='date', how='semi').sort('asset','date')
        if not market.select('date','asset').equals(state.select('date','asset')):
            raise ValueError('行情与状态主键不一致')
        if market['date'].max() != end or state['date'].max() != end:
            raise ValueError('面板实际截止日不符')
        for name, frame in [('market',market),('state',state),('calendar',calendar),('terminal_values',frames['terminal_values'])]:
            frame.write_parquet(root/f'{name}.parquet')
        print(f'历史行情和状态已发布，构建固定{horizon}日标签', flush=True)
        labels = tradable_fixed_labels(market.select('date','asset','open'), state, calendar, horizon)
        labels.write_parquet(root/'label.parquet')
        write_json(root/'release.json', release.model_dump(mode='json'))
        files = {p.name: dict(sha256=digest_file(p), bytes=p.stat().st_size) for p in root.glob('*.parquet')}
        verify()
        write_json(root/'manifest.json', dict(status='completed', release_id=release.release_id, source_release_id=c['source_release_id'],
            start=str(market['date'].min()), end=str(end), rows=market.height, files=files,
            off_calendar_rows=off.height, input_sha256=c['input_sha256'],
            label_contract=f'fixed_calendar_qualified_open_t1_t{horizon+1}_v3'))
    except Exception as error:
        write_json(root/'failure.json', dict(error=str(error)))
        raise
    return root


def normalize_terminal_recoveries(events: pl.DataFrame, prices: pl.DataFrame) -> tuple[pl.DataFrame,pl.DataFrame]:
    """依照事件前最后可退出价格的股份单位计算已核实回收，其余保留未知。"""
    needed={'security_id','event_date','available_at','delisting_total_return','cash_amount','source','action_type'}
    if needed-set(events.columns):raise ValueError('终止事件字段不完整')
    if events.select(pl.struct('security_id','event_date').is_duplicated().any()).item():raise ValueError('终止事件主键重复')
    if prices.select(pl.struct('security_id','last_session').is_duplicated().any()).item():raise ValueError('终止参考价格主键重复')
    normalized=events.sort('event_date').join_asof(prices.sort('last_session'),left_on='event_date',right_on='last_session',
        by='security_id',strategy='backward',allow_exact_matches=False,check_sortedness=False)
    reference=(pl.col('split_adjusted_close').is_finite()&(pl.col('split_adjusted_close')>0)&
        pl.col('split_adjustment_factor').is_finite()&(pl.col('split_adjustment_factor')>0)).fill_null(False)
    normalized=normalized.with_columns(pl.when(reference & pl.col('delisting_total_return').is_finite() & (pl.col('delisting_total_return')>=-1))
        .then(pl.col('split_adjusted_close')*(1+pl.col('delisting_total_return')))
        .when(reference & pl.col('cash_amount').is_finite() & (pl.col('cash_amount')>=0))
        .then(pl.col('cash_amount')*pl.col('split_adjustment_factor')).otherwise(None).alias('terminal_value'))
    known=(pl.col('terminal_value').is_finite()&(pl.col('terminal_value')>=0)&pl.col('available_at').is_not_null()).fill_null(False)
    return normalized.filter(known).select('security_id','event_date','available_at','terminal_value','source','last_session','action_type'),normalized.filter(~known)


def publish_terminal_recoveries(config_path: Path) -> Path:
    """冻结同一发布的终止事件与参考价，输出已知回收及独立未知事件表。"""
    import json
    from datetime import date,datetime,timezone
    from factor_miner.quote_research import _snapshot_code,_verify_code,_verify_inputs
    from factor_miner.research_report import write_json
    from factor_miner.ridge_strategy import file_hash
    c=json.loads(config_path.read_text());root=Path(c['output_root']);root.mkdir(parents=True,exist_ok=False)
    write_json(root/'protocol.json',c);write_json(root/'registration.json',dict(at=datetime.now(timezone.utc).isoformat(),config_sha256=file_hash(config_path)))
    try:
        if c['version']!='terminal-recoveries-v1' or c['price_basis']!='split_adjusted_price_only':raise ValueError('回收映射版本或价格单位不符')
        paths={c[k] for k in ['events_path','prices_path','state_path','source_manifest_path']}
        _verify_inputs(c,paths);code=_snapshot_code(root)
        if json.loads(Path(c['source_manifest_path']).read_text())['release_id']!=c['source_release_id']:raise ValueError('终止映射来源发布不符')
        start,end=date.fromisoformat(c['start']),date.fromisoformat(c['end'])
        state=pl.scan_parquet(c['state_path']).filter(pl.col('can_close_long')).select('session_date','security_id')
        prices=pl.scan_parquet(c['prices_path']).filter(pl.col('session_date')<=end).join(state,on=['session_date','security_id'],validate='1:1').select('security_id',pl.col('session_date').alias('last_session'),'split_adjusted_close','split_adjustment_factor').collect()
        events=pl.scan_parquet(c['events_path']).filter(pl.col('event_date').is_between(start,end)&(pl.col('available_at')<=end)).collect()
        known,unknown=normalize_terminal_recoveries(events,prices)
        known.write_parquet(root/'terminal_values.parquet');unknown.write_parquet(root/'unresolved_terminal_records.parquet')
        _verify_inputs(c,paths);_verify_code(code)
        write_json(root/'completion.json',dict(status='completed',known_events=known.height,unknown_events=unknown.height,
            terminal_values_sha256=file_hash(root/'terminal_values.parquet'),unknown_sha256=file_hash(root/'unresolved_terminal_records.parquet'),return_labels_used=False))
    except Exception as error:
        write_json(root/'failure.json',dict(error=str(error)));raise
    return root


def publish_observation_overlay(config_path: Path) -> Path:
    """追加同日笔数观察及明确的交易所研究股票池，复用原标签和成交状态。"""
    import json
    from datetime import datetime, timezone
    from factor_miner.artifact_storage import snapshot_code, reference_panel, digest_file
    from factor_miner.favor_schema import FavorDataRelease
    from factor_miner.favor_validation import require_panel
    from factor_miner.research_report import write_json
    c=json.loads(config_path.read_text());root=Path(c['output_root']);root.mkdir(parents=True,exist_ok=False)
    write_json(root/'protocol.json',c)
    write_json(root/'registration.json',dict(at=datetime.now(timezone.utc).isoformat(),config_sha256=digest_file(config_path)))
    try:
        base=Path(c['base_dataset_root'])
        names=['market.parquet','state.parquet','calendar.parquet','label.parquet','terminal_values.parquet']
        required={str(base/n) for n in [*names,'manifest.json','release.json']}|{c['source_path']}
        if required-set(c['input_sha256']):raise ValueError('观察发布来源未完整绑定')
        if c['version']!='nasdaq-trade-count-overlay-v1' or c['availability']!='after_close_t':
            raise ValueError('观察发布版本或可得时点不正确')
        if c['revision_policy']!='annual_historical_release_no_daily_vintage_guarantee':
            raise ValueError('必须声明年度历史修订边界')
        if c['universe_exchange']!='Q':raise ValueError('本观察来源仅支持Nasdaq专用研究')
        def verify():
            for path,h in c['input_sha256'].items():
                if digest_file(Path(path))!=h:raise ValueError('观察发布输入变化：'+path)
        verify();snapshot_code(root)
        manifest=json.loads((base/'manifest.json').read_text())
        for name in names:
            item=manifest['files'][name];h=item['sha256'] if isinstance(item,dict) else item
            if h!=c['input_sha256'][str(base/name)]:raise ValueError('基础清单与实际输入不符')
        release=FavorDataRelease.model_validate_json((base/'release.json').read_text())
        market=pl.read_parquet(base/'market.parquet');state=pl.read_parquet(base/'state.parquet')
        require_panel(market,{'date','asset','open','high','low','close','volume'},'原行情')
        require_panel(state,{'date','asset','valid_for_factor_rank'},'原状态')
        if market['date'].max()!=release.as_of_date or state['date'].max()!=release.state_as_of_date:
            raise ValueError('原发布截止日不符')
        if 'trade_count' in market.columns:raise ValueError('禁止覆盖原字段')
        mapping=c['source_columns']
        if set(mapping)!={'date','asset','trade_count','primary_exchange'}:raise ValueError('观察映射不完整')
        raw=pl.scan_parquet(c['source_path']).select(*(pl.col(v).alias(k) for k,v in mapping.items())).filter(
            pl.col('date').is_between(market['date'].min(),market['date'].max())).collect()
        require_panel(raw,set(mapping),'交易笔数来源')
        if not raw.schema['trade_count'].is_numeric():raise ValueError('笔数必须为数值')
        valid=(pl.col('trade_count').is_finite()&(pl.col('trade_count')>=0)&
               (pl.col('trade_count')==pl.col('trade_count').floor())).fill_null(False)
        raw=raw.with_columns(pl.when(valid).then(pl.col('trade_count')).otherwise(None).cast(pl.Float64).alias('trade_count'))
        keys=market.select('date','asset').join(raw,on=['date','asset'],how='left',validate='1:1',maintain_order='left')
        if keys['primary_exchange'].null_count():raise ValueError('行情主键缺少当日交易所身份，不能猜测股票池')
        joined=market.join(keys.select('date','asset','trade_count'),on=['date','asset'],how='left',validate='1:1',maintain_order='left')
        ranked=state.join(keys.select('date','asset','primary_exchange'),on=['date','asset'],how='left',validate='1:1',maintain_order='left')
        if ranked['primary_exchange'].null_count():raise ValueError('状态缺少交易所身份')
        ranked=ranked.with_columns((pl.col('valid_for_factor_rank')&(pl.col('primary_exchange')=='Q')).alias('valid_for_factor_rank')).drop('primary_exchange')
        if not joined.select(market.columns).equals(market):raise ValueError('观察连接改变行情')
        other=[n for n in state.columns if n!='valid_for_factor_rank']
        if not ranked.select(other).equals(state.select(other)):raise ValueError('研究范围改变成交或计算状态')
        joined.write_parquet(root/'market.parquet');ranked.write_parquet(root/'state.parquet')
        for name in names[2:]:reference_panel(base/name,root/name)
        files={name:dict(sha256=digest_file(root/name),bytes=(root/name).stat().st_size) for name in names}
        write_json(root/'release.json',release.model_copy(update=dict(release_id=c['release_id'],
            source=release.source+'；CRSP同日Nasdaq交易笔数，排名范围限当日Nasdaq',state_version=files['state.parquet']['sha256'])).model_dump(mode='json'))
        write_json(root/'manifest.json',dict(status='completed',release_id=c['release_id'],as_of_date=str(release.as_of_date),
            files=files,rows=market.height,base_dataset_root=str(base),base_input_sha256={str(base/n):c['input_sha256'][str(base/n)] for n in names},
            base_fields_unchanged=True,ranking_subset_only=True,labels_unchanged=True,revision_policy=c['revision_policy']))
        coverage=ranked.filter(pl.col('valid_for_factor_rank')).join(keys,on=['date','asset'],validate='1:1').group_by(
            pl.col('date').dt.year().alias('year')).agg(pl.len().alias('rankable_rows'),pl.col('trade_count').is_not_null().mean().alias('coverage')).sort('year')
        write_json(root/'verification.json',dict(yearly=coverage.to_dicts(),source_rows=raw.height,original_prices_unchanged=True,
            original_execution_masks_unchanged=True,return_labels_read=False,universe='当日Nasdaq且原可排名'))
        verify()
        write_json(root/'completion.json',dict(status='completed',manifest_sha256=digest_file(root/'manifest.json')))
    except Exception as error:
        write_json(root/'failure.json',dict(error=str(error)));raise
    return root
