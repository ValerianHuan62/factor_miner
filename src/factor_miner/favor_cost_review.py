"""既有探索信号的独立成本复核；不改原账本或正式确认状态。"""
from datetime import datetime, timezone, timedelta
import json
import math
from pathlib import Path

import polars as pl

from factor_miner.favor_workflow import load_plan, file_sha, verify_inputs, _trade_inputs
from factor_miner.favor_store import projection_payload
from factor_miner.favor_exploration import observation_frame
from factor_miner.favor_integration import execute_joint, portfolio_metrics
from factor_miner.research_report import write_json, performance


def research_admission(report, direction, metrics):
    """研究入库标准；正参考收益不等于实际盈利或统计通过。"""
    sign = 1 if direction == 'positive' else -1
    d = report.get('discovery', {})
    checks = dict(
        construct=report['synthetic']['status'] == '基础检验符合' and report['empirical']['passed'],
        direction=all(isinstance(d.get(k), (int, float)) and math.isfinite(d[k]) and sign*d[k] > 0
                      for k in ('ic_mean', 'rank_ic_mean')),
        cost=math.isfinite(metrics['reference']['annualized_return']) and metrics['reference']['annualized_return'] > 0)
    return dict(eligible=all(checks.values()), checks=checks, formal_pass=False,
                label='研究候选·待正式确认' if all(checks.values()) else '本次研究筛选未通过')


def load_cost_review(root: Path) -> dict:
    """读取完成身份，拒绝损坏和伪造晋级。"""
    receipt = json.loads((root/'completion.json').read_text())
    for name in ('summary','protocol'):
        if file_sha(root/(name+'.json')) != receipt[name+'_sha256']:
            raise ValueError('成本复核身份变化')
    summary = json.loads((root/'summary.json').read_text())
    if summary['version'] == 'favor-research-export-v1':
        protocol = json.loads((root/'protocol.json').read_text())
        for item in protocol['sources']:
            for path, digest in item['files'].items():
                if file_sha(Path(path)) != digest:
                    raise ValueError('复用源研究身份变化')
    if summary['formal_candidates'] or summary['test_evaluated'] or summary['sealed_oos']:
        raise ValueError('成本复核不能晋级正式确认')
    for row in summary['results']:
        path = root/row['trial_id']/'report.json'
        if file_sha(path) != receipt['reports'][row['trial_id']] or json.loads(path.read_text()) != row:
            raise ValueError('候选复核报告身份变化')
    if summary['research_candidates'] != sum(r['admission']['eligible'] for r in summary['results']):
        raise ValueError('研究候选数量不符')
    return summary


def export_exploration_candidates(config_path: Path) -> Path:
    """复用事前基准成本的完整交易诊断，不重新计算因子或回测。"""
    config = json.loads(config_path.read_text())
    if config['version'] != 'favor-research-export-v1' or not config['sources']:
        raise ValueError('研究候选导出配置不完整')
    cost = config['round_trip_cost_bps']
    if cost != 14 or config['market_id'] != 'a_share':
        raise ValueError('当前授权为A股双边万14研究候选')
    results = []; identities = set()
    for item in config['sources']:
        source = Path(item['root']).resolve(); trial = item['trial_id']
        plan = load_plan(source); summary, _, _ = projection_payload(source)
        if (plan.version != 'favor-exploration-v1' or plan.market_id != config['market_id']
                or plan.execution.round_trip_cost_bps != cost):
            raise ValueError('必须复用原计划事前冻结的同市场基准成本')
        detail_path = source/'factors'/trial/'validation_diagnostics.json'
        required = [source/'plan.json', source/'summary.json', source/'completion.json',
                    source/'submissions'/trial/'spec.json', detail_path]
        if any(str(path) not in item['files'] for path in required):
            raise ValueError('复用源身份清单不完整')
        for path, digest in item['files'].items():
            if file_sha(Path(path)) != digest: raise ValueError('复用源研究身份变化')
        report = summary['factors'][trial]
        if report['status'] != 'exploration_evaluated' or report['source_candidate_id'] in identities:
            raise ValueError('候选未评价或身份重复')
        identities.add(report['source_candidate_id'])
        details = [x for x in json.loads(detail_path.read_text()) if x['cost_bps'] == cost]
        if len(details) != 1: raise ValueError('原基准成本诊断必须唯一')
        detail = details[0]
        original = next(x['metrics'] for x in report['validation'] if x['cost_bps'] == cost)
        if detail['metrics'] != original or detail['summary']['round_trip_cost_bps'] != cost:
            raise ValueError('交易明细与冻结汇总成本或指标不一致')
        if original['actual'] is None and not detail['unresolved_positions']:
            raise ValueError('实际收益缺失必须保存未确定持仓')
        condition = next(c for c in plan.conditions if c.condition_id == report['condition_id'])
        entry = dict(trial_id=trial, source_candidate_id=report['source_candidate_id'],
            name=condition.observation.measurement, source_root=str(source),
            original_direction=condition.expected_return_sign, hypothesis=plan.hypothesis.model_dump(mode='json'),
            discovery=report['discovery'], round_trip_cost_bps=cost, metrics=original,
            old_cost_bps=cost, old_metrics=original, parity_verified=True,
            execution_reused=True, execution_source=str(detail_path),
            execution_source_sha256=file_sha(detail_path), execution_summary=detail['summary'],
            unresolved_positions=len(detail['unresolved_positions']),
            old_stress_results=report['validation'][1:],
            standalone_statistical_pass=report.get('standalone_statistical_pass'),
            admission=research_admission(report, condition.expected_return_sign, original),
            related_candidates=item.get('related_candidates', []),
            mechanism_status=plan.hypothesis.mechanism_status, test_evaluated=False, sealed_oos=False)
        candidate_formula(entry)
        results.append(entry)
    root = Path(config['output_root']); root.mkdir(parents=True, exist_ok=False)
    write_json(root/'protocol.json', config)
    for entry in results:
        write_json(root/entry['trial_id']/'report.json', entry)
    write_json(root/'summary.json', dict(version=config['version'], market_id=config['market_id'],
        round_trip_cost_bps=cost, results=results,
        research_candidates=sum(x['admission']['eligible'] for x in results),
        formal_candidates=0, test_evaluated=False, sealed_oos=False))
    write_json(root/'completion.json', dict(summary_sha256=file_sha(root/'summary.json'),
        protocol_sha256=file_sha(root/'protocol.json'),
        reports={x['trial_id']:file_sha(root/x['trial_id']/'report.json') for x in results}))
    load_cost_review(root)
    return root


def candidate_formula(row: dict) -> tuple[dict, str]:
    """从内容寻址的原始Spec取公式，不从结果反推。"""
    from factor_miner.schema import TrustedCandidateFactorSpec, registered_trusted_candidate
    path=Path(row['source_root'])/'submissions'/row['trial_id']/'spec.json'
    spec=TrustedCandidateFactorSpec.model_validate_json(path.read_text())
    if registered_trusted_candidate(spec).candidate_id != row['source_candidate_id']:
        raise ValueError('公式与候选身份不一致')
    expression=spec.expression.model_dump(mode='json',exclude_none=True)
    def render(node):
        op=node['op']
        if op=='field':return node['field']
        if op=='const':return str(node['value'])
        args=[render(a) for a in node.get('args',[])]
        symbols={'add':'+','sub':'−','mul':'×','div':'/','gt':'>'}
        if op in symbols:return '('+f' {symbols[op]} '.join(args)+')'
        args += [f'{k}={node[k]}' for k in ('window','period') if node.get(k) is not None]
        return op+'('+', '.join(args)+')'
    return expression,render(expression)


def run_cost_review(config_path: Path) -> Path:
    """冻结原始身份与唯一新成本，复现旧基线后重新运行因果引擎。"""
    config = json.loads(config_path.read_text())
    cost = config['round_trip_cost_bps']
    if not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        raise ValueError('成本必须是非负有限双边基点')
    if config['version'] != 'favor-cost-review-v1' or not config['sources']:
        raise ValueError('成本复核配置不完整')
    root = Path(config['output_root'])
    root.mkdir(parents=True, exist_ok=False)
    write_json(root/'protocol.json', config)
    def event(status, **detail):
        with (root/'events.jsonl').open('a') as f:
            f.write(json.dumps(dict(time=datetime.now(timezone.utc).isoformat(), status=status, **detail), ensure_ascii=False)+'\n')
    event('registered', historical_outcomes_seen=True, new_hypotheses=0)
    try:
        from factor_miner.artifact_storage import snapshot_code
        snapshot_code(root)
        # 一次验证同一数据发布；每个源仍必须绑定相同哈希。
        inputs, sources, identities = None, [], set()
        for item in config['sources']:
            source = Path(item['root']); plan = load_plan(source)
            summary, _, _ = projection_payload(source)
            if plan.version != 'favor-exploration-v1' or plan.market_id != config['market_id']:
                raise ValueError('只接受同市场已完成探索')
            for path, expected in item['files'].items():
                if file_sha(Path(path)) != expected:
                    raise ValueError('源研究身份变化：'+path)
            report = summary['factors'][item['trial_id']]
            if report['status'] != 'exploration_evaluated' or report['source_candidate_id'] in identities:
                raise ValueError('候选未评价或身份重复')
            identities.add(report['source_candidate_id'])
            if inputs is None:
                verify_inputs(plan); inputs = plan.input_sha256
            elif inputs != plan.input_sha256:
                raise ValueError('本次复核需要同一标准数据发布')
            raw_path = str(Path(report['raw_path']).resolve())
            if raw_path not in item['files']:
                raise ValueError('原始因子矩阵未冻结')
            sources.append((item, plan, report))
        dataset = Path(sources[0][1].dataset_root)
        # 验证末日是信号边界；退出可以更晚，但必须早于测试起点。
        end = max(p.splits.test_start for _, p, _ in sources) - timedelta(days=1)
        market = pl.scan_parquet(dataset/'market.parquet').filter(pl.col('date') <= end).collect()
        state = pl.scan_parquet(dataset/'state.parquet').filter(pl.col('date') <= end).collect()
        days = pl.read_parquet(dataset/'calendar.parquet')['date'].to_list()
        results = []
        for item, plan, report in sources:
            trial = item['trial_id']; split = plan.splits
            condition = next(c for c in plan.conditions if c.condition_id == report['condition_id'])
            trade = _trade_inputs(plan, market, state, days, split.validation_start, split.validation_end, split.test_start)
            terminals = pl.read_parquet(plan.terminal_events_path) if plan.terminal_events_path else None
            benchmark_signals = state.filter(pl.col('valid_for_factor_compute') & pl.col('valid_for_factor_rank')).select('date','asset',pl.lit(True).alias('trigger'))
            benchmark = execute_joint(benchmark_signals, trade[1], trade[2], trade[0], cost_bps=0.,
                terminal_policy=plan.execution.terminal_policy, terminal_events=terminals)
            values = pl.read_parquet(report['raw_path'])
            signals = observation_frame(values, state, days, plan.exploration[condition.condition_id], condition.activation_direction).select('date','asset',pl.col('_trigger').alias('trigger'))
            old_cost = plan.execution.round_trip_cost_bps
            baseline = execute_joint(signals, trade[1], trade[2], trade[0], cost_bps=old_cost,
                terminal_policy=plan.execution.terminal_policy, terminal_events=terminals)
            original = next(x['metrics'] for x in report['validation'] if x['cost_bps'] == old_cost)
            calculated = portfolio_metrics(baseline, benchmark)
            for k, v in original['reference'].items():
                if not math.isclose(v, calculated['reference'][k], rel_tol=1e-9, abs_tol=1e-11):
                    raise ValueError(f'{trial}原成本基线不一致：{k}')
            result = execute_joint(signals, trade[1], trade[2], trade[0], cost_bps=cost,
                terminal_policy=plan.execution.terminal_policy, terminal_events=terminals)
            metrics = portfolio_metrics(result, benchmark)
            output = root/trial; output.mkdir()
            for name in ('daily_returns','orders','selections','unresolved_positions','zero_recovery_daily_returns'):
                write_json(output/(name+'.json'), getattr(result,name))
            entry = dict(trial_id=trial, source_candidate_id=report['source_candidate_id'],
                name=condition.observation.measurement, source_root=item['root'], original_direction=condition.expected_return_sign,
                hypothesis=plan.hypothesis.model_dump(mode='json'), discovery=report['discovery'],
                round_trip_cost_bps=cost, metrics=metrics, old_cost_bps=old_cost, old_metrics=original,
                parity_verified=True, unresolved_positions=len(result.unresolved_positions),
                zero_recovery_metrics=performance([dict(r, benchmark_return={b['exit_date']:b['target_long_net_return'] for b in benchmark.daily_returns}[r['exit_date']]) for r in result.zero_recovery_daily_returns]),
                old_stress_results=report['validation'][1:], standalone_statistical_pass=report.get('standalone_statistical_pass'),
                admission=research_admission(report,condition.expected_return_sign,metrics),
                related_candidates=item.get('related_candidates',[]), mechanism_status='mechanism_unverified',
                test_evaluated=False, sealed_oos=False)
            write_json(output/'report.json',entry); results.append(entry)
            event('candidate_completed',trial_id=trial, eligible=entry['admission']['eligible'])
            print(json.dumps(dict(trial=trial,annualized=metrics['reference']['annualized_return'],eligible=entry['admission']['eligible']),ensure_ascii=False),flush=True)
            del baseline, result, benchmark, values, signals
        # 复核结束再检查输入未变化；原结果不改写。
        for item, _, _ in sources:
            for path, expected in item['files'].items():
                if file_sha(Path(path)) != expected: raise ValueError('运行期间源研究变化')
        write_json(root/'summary.json',dict(version=config['version'],market_id=config['market_id'],
            round_trip_cost_bps=cost,results=results,research_candidates=sum(x['admission']['eligible'] for x in results),
            formal_candidates=0,test_evaluated=False,sealed_oos=False))
        write_json(root/'completion.json',dict(summary_sha256=file_sha(root/'summary.json'),
            protocol_sha256=file_sha(root/'protocol.json'), reports={x['trial_id']:file_sha(root/x['trial_id']/'report.json') for x in results}))
        event('completed'); return root
    except Exception as error:
        event('failed',reason=str(error)); raise
