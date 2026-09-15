"""组合研究资格与单因子显著性分离；历史失败不充当有效替代品。"""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path
import json
import math
import time

import polars as pl

from factor_miner.compiler import attach_market_sessions, build_polars_expr, compile_candidate
from factor_miner.dsl import iter_ast_nodes
from factor_miner.schema import TrustedCandidateFactorSpec, registered_trusted_candidate
from factor_miner.research_report import write_json, ic_diagnostics
from factor_miner.ridge_strategy import file_hash


def research_admission(report: dict, *, coverage_min: float = .8,
                       exclusions: tuple[str, ...] = ()) -> dict:
    """只使用发现期；允许弱边际信号，不挽救反方向或测量失败。"""
    discovery = report['discovery']
    for key in ('rank_ic_mean', 'rank_ic_hac_t', 'median_coverage'):
        value = discovery.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{report['factor_id']} 缺少发现期 {key}")
    direction = report['hypothesis_direction']
    if direction not in {'positive', 'negative'}:
        raise ValueError('缺少原假设方向')
    reasons = list(exclusions)
    if report['construct_validation']['status'] != '基础检验符合':
        reasons.append('原构念检验未符合')
    if discovery['median_coverage'] < coverage_min:
        reasons.append('发现期覆盖不足')
    if discovery['rank_ic_mean'] * (1 if direction == 'positive' else -1) <= 0:
        reasons.append('发现期原方向无正向预测信息，不翻转假设')
    return dict(status='excluded' if reasons else 'eligible', reasons=reasons,
                source_candidate_id=report['candidate_id'], original_direction=direction,
                standalone_statistical_pass_unchanged=True, mechanism_status='mechanism_unverified')


def select_pool(reports: list[dict], admissions: dict, pair_measure,
                *, anchors: list[str], threshold: float = .75) -> tuple[list[str], list[dict]]:
    """锚定既有组合后按发现强度选择；只对当前有效代表判定冗余。"""
    by_id = {r['factor_id']: r for r in reports}
    if len(by_id) != len(reports) or len(set(anchors)) != len(anchors):
        raise ValueError('候选或基准编号重复')
    if any(fid not in by_id or admissions[fid]['status'] != 'eligible' for fid in anchors):
        raise ValueError('固定组合基准包含不可研究候选')
    remaining = sorted((r for r in reports if admissions[r['factor_id']]['status'] == 'eligible'
                        and r['factor_id'] not in anchors),
                       key=lambda r: (-abs(r['discovery']['rank_ic_hac_t']), r['factor_id']))
    selected, pairs = [], []
    for current in [*(by_id[fid] for fid in anchors), *remaining]:
        fid = current['factor_id']
        for prior_id in selected:
            prior = by_id[prior_id]
            same_ast = current['ast_hash'] == prior['ast_hash']
            pair = (dict(valid_dates=None, p95_abs_spearman=1.) if same_ast
                    else pair_measure(fid, prior_id))
            pairs.append(dict(factor_id=fid, reference=prior_id, same_ast=same_ast, **pair))
            similarity = pair['p95_abs_spearman']
            if similarity is None:
                raise ValueError(f'{fid}/{prior_id} 重叠不足，不能认定独立')
            if same_ast or similarity >= threshold:
                if fid in anchors:
                    raise ValueError('冻结基准内部存在冗余，须重新冻结明确名单')
                admissions[fid].update(status='redundant', representative=prior_id,
                    reasons=['与当前有效组合代表高度相关；历史失败相似性不作为本项否决'])
                break
        else:
            selected.append(fid)
            admissions[fid].update(status='research_input', reasons=['可进入冻结组合研究；不是单因子显著性升级'])
    return selected, pairs


def validate_search_budget(plan: dict, proposals: list[dict]) -> dict:
    """有限预算按信息来源及机制消费，失败与窗口变体也占名额。"""
    cap, model_cap = plan['candidate_capacity'], plan['model_capacity']
    if type(cap) is not int or type(model_cap) is not int or min(cap, model_cap) < 1:
        raise ValueError('候选与模型预算必须为正整数')
    if len(proposals) > cap or len({p['trial_id'] for p in proposals}) != len(proposals):
        raise ValueError('试验超过冻结容量或身份重复')
    mechanisms = plan['mechanisms']
    if any(type(m['capacity']) is not int or m['capacity'] < 1 or type(m['data_ready']) is not bool for m in mechanisms.values()):
        raise ValueError('机制配额必须为正整数，可用性必须为布尔值')
    if sum(m['capacity'] for m in mechanisms.values()) > cap:
        raise ValueError('机制配额超过总预算')
    used = Counter()
    for proposal in proposals:
        mechanism = mechanisms.get(proposal['mechanism_id'])
        if mechanism is None or proposal['information_source'] != mechanism['information_source']:
            raise ValueError('假设机制或信息来源不符合冻结计划')
        if not mechanism['data_ready'] or not mechanism['horizon_rationale']:
            raise ValueError('机制缺少可用数据或持有期依据，不能用价量代理补位')
        used[proposal['mechanism_id']] += 1
        if used[proposal['mechanism_id']] > mechanism['capacity']:
            raise ValueError('同机制变体超过冻结配额')
    inherited = plan['inherited_diagnostic_family_size']
    history_status = plan.get('historical_budget_status', 'complete')
    if history_status not in {'complete', 'unrecoverable'}:
        raise ValueError('未知历史预算状态')
    unknown_history = history_status == 'unrecoverable'
    if unknown_history:
        if (plan.get('version') != 'bounded-research-v2'
                or plan.get('validation_target') != 'signal_exploration'
                or not isinstance(plan.get('history_loss_reason'), str)
                or not plan['history_loss_reason'].strip()
                or inherited is not None or plan['historical_attempt_count'] is not None):
            raise ValueError('历史预算不可恢复仅允许显式说明原因的探索；历史数量必须为 null')
    elif any(type(plan[k]) is not int or plan[k] < 0 for k in ('historical_attempt_count', 'inherited_diagnostic_family_size')):
        raise ValueError('历史预算必须为非负整数')
    if not unknown_history and inherited < plan['historical_attempt_count']:
        raise ValueError('不得缩小历史试验或已有诊断预算')
    if plan.get('version') not in {None, 'bounded-research-v2'}:
        raise ValueError('未知有限研究预算版本')
    if plan.get('version') == 'bounded-research-v2':
        for key in ('research_question', 'stop_rule', 'primary_comparison', 'information_gain_rationale'):
            if not isinstance(plan.get(key), str) or not plan[key].strip():
                raise ValueError(f'有限研究缺少 {key}')
        limits = plan.get('resource_limits', {})
        if set(limits) != {'model_responses', 'output_tokens', 'wall_seconds', 'artifact_bytes'}:
            raise ValueError('必须冻结模型响应、输出 Token、时间和产物字节上限')
        if any((type(value) is not int or value <= 0)
               and not (key == 'output_tokens' and value is None)
               for key, value in limits.items()):
            raise ValueError('资源上限必须为正整数；仅输出 Token 可显式为 null 表示不限量')
        if plan.get('validation_target') not in {'joint_trigger', 'portfolio_increment', 'signal_exploration'}:
            raise ValueError('必须区分信号探索、联合触发验证与组合增量对照')
    return dict(consumed_candidates=len(proposals), remaining_candidates=cap-len(proposals),
                mechanism_consumption=dict(used), diagnostic_family_size=None if unknown_history else inherited+cap+model_cap)


def check_resource_usage(plan: dict, usage: dict) -> dict:
    """按调用方提供的累计计量停止后续工作；不把缓存输入或模型拟合数冒充输出 Token。"""
    if plan.get('version') != 'bounded-research-v2':
        raise ValueError('资源检查要求 bounded-research-v2')
    validate_search_budget(plan, [])
    limits = plan['resource_limits']
    if set(usage) != set(limits) or any(type(v) is not int or v < 0 for v in usage.values()):
        raise ValueError('累计资源计量缺失或非法；不能按零处理未知用量')
    exhausted = [key for key in limits if limits[key] is not None and usage[key] >= limits[key]]
    return dict(may_continue=not exhausted, exhausted=exhausted,
                remaining={key: None if limits[key] is None else max(0, limits[key]-usage[key]) for key in limits},
                metering_source='caller_reported_cumulative_usage')


class ResearchResourceGuard:
    """在阶段边界检查本次运行耗时与实际写入量；模型用量由调用方独立计量。"""

    def __init__(self, plan: dict, root: Path, *, external_check=None):
        self.limits = plan.get('resource_limits') if plan.get('version') == 'bounded-research-v2' else None
        self.root = root
        self.started = time.monotonic()
        self.external_check = external_check

    def check(self) -> None:
        if self.external_check is not None:
            self.external_check()
        if self.limits is None:
            return
        elapsed = time.monotonic() - self.started
        # 不追踪 code 与上游数据链接；共享对象由发布方计量一次。
        size = sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file() and not p.is_symlink())
        if elapsed >= self.limits['wall_seconds'] or size >= self.limits['artifact_bytes']:
            raise ValueError(f'研究资源上限达到，停止后续阶段：elapsed_seconds={elapsed:.3f}, artifact_bytes={size}')


def rebuild_raw(report: dict, origin: dict, dataset: Path, destination: Path) -> None:
    """原 Spec 在同一冻结发布上重建矩阵，保留原119观察预热语义。"""
    from factor_miner.market_context import attach_market_context
    from factor_miner.dispersion_context import attach_dispersion_context
    from factor_miner.vix_context import attach_vix_context, attach_vvix_context, attach_ovx_context
    spec = TrustedCandidateFactorSpec.model_validate_json(Path(report['spec_path']).read_text())
    registered = registered_trusted_candidate(spec)
    if registered.candidate_id != report['candidate_id']:
        raise ValueError('原 Spec 身份不符')
    compile_candidate(registered, set(spec.required_fields))
    market = pl.scan_parquet(dataset/'market.parquet')
    for field, prefix, attach in (
        ('market_return', 'market', attach_market_context),
        ('vix_change', 'vix', attach_vix_context), ('vvix_change', 'vvix', attach_vvix_context),
        ('ovx_change', 'ovx', attach_ovx_context), ('dispersion_change', 'dispersion', attach_dispersion_context)):
        if field in spec.required_fields:
            market = attach(market, dataset/'calendar.parquet', Path(origin[f'{prefix}_context_path']),
                            Path(origin[f'{prefix}_context_contract_path']))
    if any(n.op in {'calendar_delay', 'calendar_delta'} for n in iter_ast_nodes(spec.expression)):
        market = attach_market_sessions(market, pl.read_parquet(dataset/'calendar.parquet'))
    raw = market.sort('date', 'asset').with_columns(build_polars_expr(spec.expression).alias('raw_factor'),
        (pl.col('date').cum_count().over('asset') > 119).alias('warmup')).join(
        pl.scan_parquet(dataset/'state.parquet').select('date', 'asset', 'valid_for_factor_compute'),
        on=['date', 'asset'], validate='1:1').select('date', 'asset', 'raw_factor',
        (pl.col('warmup') & pl.col('valid_for_factor_compute')).alias('valid_for_factor_compute'))
    raw.sink_parquet(destination)


def run_research_pool(config_path: Path) -> Path:
    """登记新复核版本，恢复必要矩阵，只在发现期建立组合研究名单。"""
    config = json.loads(config_path.read_text())
    root = Path(config['output_root'])
    root.mkdir(parents=True, exist_ok=False)
    write_json(root/'protocol.json', config)
    write_json(root/'registration.json', dict(status='registered_before_new_review',
        test_consumed=True, sealed_oos=False, scope='历史探索性重新分层；原单因子统计不改'))
    for source, expected in config['input_sha256'].items():
        if file_hash(Path(source)) != expected:
            raise ValueError(f'冻结输入变化：{source}')
    from factor_miner.artifact_storage import snapshot_code
    snapshot_code(root)
    previous = json.loads(Path(config['previous_review_path']).read_text())
    reports, origins, exclusions = {}, {}, {}
    for source in config['source_configs']:
        origin = json.loads(Path(source).read_text())
        if origin['dataset_root'] != config['dataset_root']:
            raise ValueError('历史报告不是同一数据发布')
        manifest_path = Path(origin['output_root'])/'run_manifest.json'
        if str(manifest_path) not in config['input_sha256']:
            raise ValueError('历史报告身份未冻结')
        for report in json.loads(manifest_path.read_text())['reports']:
            fid = report['factor_id']
            if fid in reports:
                if reports[fid]['candidate_id'] != report['candidate_id']:
                    raise ValueError('稳定编号身份冲突')
                continue
            if str(Path(report['spec_path'])) not in config['input_sha256']:
                raise ValueError('原 Spec 未绑定')
            reports[fid], origins[fid] = report, origin
            exclusions[fid] = []
    if set(reports) != set(previous['by_factor']) or len(reports) != config['evaluated_count']:
        raise ValueError('全历史复核覆盖不完整')
    semantic = json.loads(Path(config['semantic_audit_path']).read_text())['by_factor']
    for fid, audit in semantic.items():
        if audit['source_candidate_id'] != reports[fid]['candidate_id']:
            raise ValueError('语义审计身份不符')
        if audit['semantic_hold']:
            exclusions[fid].append(audit['semantic_hold_reason'])
    for source in config['measurement_admissions']:
        for audit in json.loads(Path(source).read_text())['items']:
            if audit['candidate_id'] != reports[audit['factor_id']]['candidate_id']:
                raise ValueError('测量审计身份不符')
            exclusions[audit['factor_id']] += audit['reasons']
    admissions = {fid: research_admission(r, coverage_min=config['coverage_min'],
        exclusions=tuple(exclusions[fid])) for fid, r in reports.items()}
    write_json(root/'admission.json', admissions)
    dataset = Path(config['dataset_root'])
    start, end = (date.fromisoformat(config[k]) for k in ('discovery_start', 'discovery_end'))
    state = pl.scan_parquet(dataset/'state.parquet')
    mask = state.filter(pl.col('date').is_between(start, end) & pl.col('valid_for_factor_rank')).select('date', 'asset')
    paths, panels, restoration = {}, {}, []
    restored = root/'restored'; restored.mkdir()
    from factor_miner.research_report import report_schedule
    days = pl.read_parquet(dataset/'calendar.parquet')['date'].to_list()
    weekly = [w.signal_date for w in report_schedule(days, start, end, 'weekly_last_session')]
    for fid, admission in admissions.items():
        if admission['status'] != 'eligible':
            continue
        report, origin = reports[fid], origins[fid]
        path = Path(report['folder'])/'raw_factor.parquet'
        rebuilt = not path.is_file()
        print(f'研究池矩阵 {fid}：' + ('按原式恢复' if rebuilt else '使用原矩阵'), flush=True)
        if rebuilt:
            path = restored/f'{fid}.parquet'
            rebuild_raw(report, origin, dataset, path)
            _, metrics = ic_diagnostics(pl.scan_parquet(path).filter(pl.col('date').is_in(weekly)), state,
                pl.scan_parquet(dataset/'label.parquet'), start, end,
                date.fromisoformat(origin['confirmation_start']), origin['family_size'])
            for key in ('ic_mean', 'rank_ic_mean', 'rank_ic_hac_t', 'median_coverage'):
                if not math.isclose(metrics[key], report['discovery'][key], rel_tol=1e-8, abs_tol=1e-10):
                    raise ValueError(f'{fid} 恢复矩阵与原发现期统计不一致：{key}')
        paths[fid] = str(path)
        restoration.append(dict(factor_id=fid, rebuilt=rebuilt, path=str(path), sha256=file_hash(path)))
        # 每个面板只保存发现期有效值；后续逐配对按真实交集重新排名。
        target = root/'discovery_panels'/f'{fid}.parquet'; target.parent.mkdir(exist_ok=True)
        mask.join(pl.scan_parquet(path).filter(pl.col('valid_for_factor_compute') & pl.col('raw_factor').is_finite())
                  .select('date', 'asset', 'raw_factor'), on=['date', 'asset'], validate='1:1').sink_parquet(target)
        panels[fid] = target
    write_json(root/'restoration.json', restoration)
    def measure(left: str, right: str) -> dict:
        daily = pl.scan_parquet(panels[left]).join(pl.scan_parquet(panels[right]).rename({'raw_factor':'prior'}),
            on=['date','asset'], validate='1:1').group_by('date').agg(pl.len().alias('names'),
            pl.corr('raw_factor','prior',method='spearman').abs().alias('corr')).filter(
            (pl.col('names') >= 20) & pl.col('corr').is_finite()).collect()
        value = float(daily['corr'].quantile(.95)) if daily.height >= 60 else None
        print(f'有效代表查重 {left}/{right}：{value}', flush=True)
        return dict(valid_dates=daily.height, p95_abs_spearman=value)
    selected, pairs = select_pool(list(reports.values()), admissions, measure,
        anchors=config['baseline_factor_ids'], threshold=config['correlation_threshold'])
    # 已有历史相似性原样保留，与当前研究池冗余分开报告。
    for fid, entry in admissions.items():
        old = previous['by_factor'][fid]
        entry.update(previous_status=old['status'], name=old['display_name'],
                     historical_representative=old.get('representative'),
                     historical_reasons=old['reasons'], original_statistical_pass=old['statistical_pass'])
    write_json(root/'selection.json', dict(schema_version='research-pool-v1', market_id=config['market_id'],
        selected_factor_ids=selected, baseline_factor_ids=config['baseline_factor_ids'],
        addition_factor_ids=[f for f in selected if f not in config['baseline_factor_ids']],
        by_factor=admissions, pairs=pairs, raw_paths=paths, reports=reports,
        counts=dict(Counter(x['status'] for x in admissions.values())),
        test_consumed=True, sealed_oos=False, selection_uses_confirmation=False,
        inherited_diagnostic_family_size=config['inherited_diagnostic_family_size']))
    print('组合研究代表：', selected, flush=True)
    return root
