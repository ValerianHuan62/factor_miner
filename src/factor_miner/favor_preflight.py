"""候选生成前检查测量合同自洽性；不打开行情或收益标签。"""
from datetime import datetime, timezone
from pathlib import Path
import json
from factor_miner.canonical import sha256_json
from factor_miner.construct_validation import validate_construct
from factor_miner.favor_schema import FavorPlan
from factor_miner.errors import FactorMinerError
from factor_miner.favor_workflow import candidate_hypothesis
from factor_miner.hypothesis_constraints import compile_gamma
from factor_miner.research_report import write_json


def inspect_measurement_contracts(plan: FavorPlan) -> dict:
    """用事前状态锚检查响应方向，测量与候选单调反向时显式镜像。"""
    rows=[]
    for condition in plan.conditions:
        try:
            gamma=compile_gamma(candidate_hypothesis(plan,condition),condition,plan.allowed_fields,version=plan.gamma_version)
        except (ValueError, FactorMinerError) as error:
            rows.append(dict(condition_id=condition.condition_id,passed=False,stage='structure',reason=str(error)))
            continue
        for measurement in condition.state_measurements:
            response=condition.observation.expected_response
            if measurement.expected_direction=='decrease':
                response='decrease' if response=='increase' else 'increase'
            observation=condition.observation.model_copy(update={'expected_response':response})
            result=validate_construct(measurement.expression,observation,
                calendar_months=plan.version in {'favor-gamma-v3', 'favor-regime-v1', 'favor-exploration-v1'},
                response_aggregation='directional_changes' if plan.version == 'favor-exploration-v1' and plan.exploration[condition.condition_id].kind != 'continuous' else 'median')
            rows.append(dict(condition_id=condition.condition_id,measurement=measurement.name,
                gamma_sha256=gamma.identity,expected_measurement_response=response,
                passed=result['status']=='基础检验符合',stage='synthetic_measurement_contract',checks=result['checks']))
    return dict(plan_sha256=sha256_json(plan.model_dump(mode='json')),passed=bool(rows) and all(x['passed'] for x in rows),
        measurements=rows,return_labels_used=False,real_market_data_used=False,
        scope='仅检查事前状态锚与合成扰动方向是否自洽；不证明候选构念、经济机制或收益。失败须解释代理差异或在候选提交前修正合同，不能自动翻转收益方向。')


def inspect_data_feasibility(plan: FavorPlan) -> dict:
    """只读取发现期及预热行情、状态和日历；状态锚不能冒充尚未生成的候选。"""
    import polars as pl
    from factor_miner.compiler import attach_market_sessions, build_polars_expr
    from factor_miner.favor_workflow import file_sha
    from factor_miner.favor_validation import require_panel
    from factor_miner.favor_integration import percentile_scores, joint_signals, selectivity_feasibility

    dataset = Path(plan.dataset_root)
    frames = {}
    for name in ('market', 'state', 'calendar'):
        path = dataset / f'{name}.parquet'
        if plan.input_sha256.get(str(path)) != file_sha(path):
            raise ValueError(f'可行性预检输入未冻结或变化：{name}')
        scan = pl.scan_parquet(path)
        # 完整日历仅用于确认最后一周/月是否结束，不读取未来行情或收益。
        if name != 'calendar' or plan.version != 'favor-exploration-v1':
            scan = scan.filter(pl.col('date') <= plan.splits.discovery_end)
        frames[name] = scan.collect()
    market, state, calendar = (frames[name] for name in ('market', 'state', 'calendar'))
    require_panel(market, {'date', 'asset', *plan.allowed_fields}, '预检行情')
    require_panel(state, {'date', 'asset', 'valid_for_factor_compute', 'valid_for_factor_rank'}, '预检状态')
    for key in ('valid_for_factor_compute', 'valid_for_factor_rank'):
        if state.schema[key] != pl.Boolean or state[key].null_count():
            raise ValueError('预检 mask 必须为非空布尔值')
    days = calendar['date'].to_list()
    if days != sorted(set(days)) or not set(market['date'].to_list()).issubset(days):
        raise ValueError('预检交易日历重复、乱序或缺少行情日')
    market = attach_market_sessions(market.lazy(), calendar, calendar_months=plan.version in {'favor-gamma-v3', 'favor-regime-v1', 'favor-exploration-v1'}).collect().sort('asset', 'date')
    split = plan.splits
    universe = state.filter(pl.col('date').is_between(split.discovery_start, split.discovery_end) & pl.col('valid_for_factor_rank'))
    counts = universe.group_by('date').len(name='universe')
    if not counts.height:
        raise ValueError('发现期没有可排名股票')
    reports, scores = [], {}
    for condition in plan.conditions:
        for index, measurement in enumerate(condition.state_measurements):
            raw = market.with_columns(build_polars_expr(measurement.expression).alias('raw_factor')).join(
                state.select('date', 'asset', 'valid_for_factor_compute'), on=['date', 'asset'], how='left', validate='1:1')
            if raw['valid_for_factor_compute'].null_count():
                raise ValueError('预检行情缺少计算状态')
            raw = raw.select('date', 'asset', 'raw_factor', 'valid_for_factor_compute')
            if plan.version == 'favor-exploration-v1':
                # 状态锚只作输入覆盖提示，不冒充离散候选，更不预先要求联合收益结构。
                visible = raw.filter(pl.col('date').is_between(split.discovery_start, split.discovery_end)
                    & pl.col('valid_for_factor_compute') & pl.col('raw_factor').is_finite()).join(
                    universe.select('date','asset'), on=['date','asset'], validate='1:1')
                from factor_miner.favor_exploration import observation_dates
                dates = observation_dates(calendar['date'].to_list(), plan.exploration[condition.condition_id])
                measured_counts = counts.filter(pl.col('date').is_in(dates))
                coverage = measured_counts.join(visible.group_by('date').len(name='usable'), on='date', how='left').select(
                    (pl.col('usable').fill_null(0)/pl.col('universe')).median()).item() if measured_counts.height else None
                reports.append(dict(condition_id=condition.condition_id, measurement=measurement.name,
                    proxy_coverage=coverage, coverage_warning=coverage is None or coverage < plan.exploration[condition.condition_id].min_signal_coverage))
                continue
            # 状态锚与候选的增减关系也参与方向映射。
            activation = condition.activation_direction
            if measurement.expected_direction == 'decrease':
                activation = 'low' if activation == 'high' else 'high'
            score = percentile_scores(raw, state, activation)
            visible = score.filter(pl.col('date').is_between(split.discovery_start, split.discovery_end))
            coverage = counts.join(visible.group_by('date').len(name='usable'), on='date', how='left').select(
                (pl.col('usable').fill_null(0)/pl.col('universe')).median()).item()
            reports.append(dict(condition_id=condition.condition_id, measurement=measurement.name,
                proxy_coverage=coverage, coverage_warning=coverage < plan.construct_policy.min_signal_coverage))
            if index == 0:
                scores[condition.condition_id] = score
    if plan.version == 'favor-exploration-v1':
        return dict(measurements=reports, return_labels_used=False,
            needs_review=any(r['coverage_warning'] for r in reports),
            scope='探索状态锚覆盖提示；正式候选按类型检查，未要求五组、联合三档或逐股重复事件')
    members = tuple(scores)
    ladder = {q: joint_signals(scores, members, (q,)*len(members)) for q in plan.integration.selectivity_levels}
    feasibility = selectivity_feasibility(ladder, start=split.discovery_start, end=split.discovery_end,
        min_events=plan.integration.min_events_per_ticker, min_tickers=plan.integration.min_tickers,
        support_threshold=plan.integration.ticker_support_threshold)
    return dict(measurements=reports, proxy_joint_events=feasibility, return_labels_used=False,
                needs_review=any(r['coverage_warning'] for r in reports) or not feasibility['feasible'],
                scope='各条件首个事前状态锚的联合事件提示；不代替正式候选，不能据此宣称收益失败或通过')


def preflight_favor(config_path: Path, output_path: Path, *, with_data: bool = False) -> dict:
    """正式只读预检入口，输出不可覆盖且不创建研究登记或候选名额。"""
    from factor_miner.favor_schema import parse_favor_plan
    payload = json.loads(config_path.read_text())
    if payload.get('version') == 'favor-monthly-state-preflight-v1':
        from factor_miner.monthly_volume_state import preflight_monthly_volume
        return preflight_monthly_volume(payload, output_path, with_data=with_data)
    plan=parse_favor_plan(payload)
    report=inspect_measurement_contracts(plan)
    if with_data and report['passed']:
        report['data_feasibility'] = inspect_data_feasibility(plan)
        report['real_market_data_used'] = True
        report['needs_review'] = report['data_feasibility']['needs_review']
    write_json(output_path,dict(checked_at=datetime.now(timezone.utc).isoformat(),**report))
    return dict(passed=report['passed'], needs_review=report.get('needs_review', False),
        output_path=str(output_path),plan_sha256=report['plan_sha256'],measurements=len(report['measurements']))
