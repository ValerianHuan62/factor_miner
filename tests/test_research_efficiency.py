"""可行性检查先于收益，资源有限，共享产物身份可验证。"""
from datetime import date, timedelta
from pathlib import Path
import json

import polars as pl
import pytest
from typer.testing import CliRunner

from factor_miner.artifact_storage import snapshot_code, reference_panel
from factor_miner.cli import app
from factor_miner.favor_demo import make_plan, expressions
from factor_miner.favor_integration import selectivity_feasibility, directional_selectivity
from factor_miner.favor_preflight import inspect_data_feasibility
from factor_miner.favor_workflow import register_favor, submit_favor, favor_generation_payload, run_favor
from factor_miner.research_pool import validate_search_budget, check_resource_usage


def bounded(budget):
    return dict(budget, version='bounded-research-v2', research_question='固定组合是否缺少修复信息',
        validation_target='joint_trigger', primary_comparison='共同截面的原组合与新增组合', information_gain_rationale='新增独立观察',
        stop_rule='本轮完成或任一资源上限达到即停止',
        resource_limits=dict(model_responses=10, output_tokens=20000, wall_seconds=600, artifact_bytes=100000000))


def real_plan_data(plan):
    """资源闸门测试使用当前真实计划版本，避免被更早的版本闸门截断。"""
    from factor_miner.regime import RegimeHypothesisSpec
    state = RegimeHypothesisSpec(has_claim=False, claim='本合成测试无额外状态主张',
        dimension='none', direction='none', effect='none', scope='none', measurement_ref='',
        measurement_definition='', required_fields=(), availability='', lag_sessions=0,
        threshold_rule='', data_available=True, unavailable_reason='', control_state='',
        failure_condition='', falsification='', competing_explanations=())
    return dict(plan.model_dump(mode='json'), run_kind='research', version='favor-regime-v1',
        regimes={c.condition_id: state.model_dump(mode='json') for c in plan.conditions})


def test_snapshot_reuses_content_without_linking_mutable_working_code(tmp_path, monkeypatch):
    source = tmp_path/'source'; source.mkdir(); (source/'a.py').write_text('x = 1\n')
    monkeypatch.setenv('FACTOR_MINER_CODE_STORE', str(tmp_path/'shared'))
    roots = [tmp_path/'one', tmp_path/'nested'/'two']
    for root in roots:
        root.mkdir(parents=True)
        snapshot_code(root, source=source)
    assert (roots[0]/'code').resolve() == (roots[1]/'code').resolve()
    (source/'a.py').write_text('x = 2\n')
    assert (roots[0]/'code'/'a.py').read_text() == 'x = 1\n'
    third = tmp_path/'three'; third.mkdir(); snapshot_code(third, source=source)
    assert (third/'code').resolve() != (roots[0]/'code').resolve()
    broken = third/'code'/'a.py'; broken.chmod(0o644); broken.write_text('corrupt')
    fourth = tmp_path/'four'; fourth.mkdir()
    with pytest.raises(ValueError, match='损坏'):
        snapshot_code(fourth, source=source)


def test_upstream_reference_is_explicit_and_never_overwrites(tmp_path):
    source = tmp_path/'source.parquet'; source.write_bytes(b'input')
    target = tmp_path/'ref.parquet'; reference_panel(source, target)
    assert target.is_symlink() and target.resolve() == source
    with pytest.raises(FileExistsError):
        reference_panel(source, target)


def test_event_ceiling_fails_without_accessing_labels():
    start = date(2021, 1, 1)
    frame = pl.DataFrame([dict(date=start+timedelta(days=i), asset=a, trigger=True) for i in range(8) for a in 'ABCD'])
    ladder = {.5: frame, .7: frame, .9: frame.with_columns((pl.col('asset') == 'A').alias('trigger'))}
    kwargs = dict(start=start, end=start+timedelta(days=7), min_events=5, min_tickers=2, support_threshold=.5)
    report = selectivity_feasibility(ladder, **kwargs)
    assert report['support_upper_bound'] == .25 and not report['feasible']
    result = directional_selectivity(ladder, None, boundary=date(2022, 1, 1), **kwargs)
    assert result['status'] == 'insufficient_event_capacity' and not result['return_labels_used']
    assert result['ticker_support_rate'] is None
    with pytest.raises(ValueError, match='独立后续分区'):
        directional_selectivity(ladder, None, boundary=start, **kwargs)


def test_data_preflight_never_reads_labels(tmp_path):
    plan, _ = make_plan(tmp_path)
    label = Path(plan.dataset_root)/'label.parquet'
    label.unlink()
    report = inspect_data_feasibility(plan)
    assert not report['return_labels_used'] and report['measurements']
    assert 'proxy_joint_events' in report
    assert not list(tmp_path.glob('**/ledger'))


def test_budget_cli_stops_at_limit_without_resetting_statistical_history(tmp_path):
    plan, _ = make_plan(tmp_path)
    budget = bounded(plan.search_budget)
    proposals = [s.model_dump(exclude={'condition_id'}) for s in plan.slots]
    original = validate_search_budget(plan.search_budget, proposals)
    assert validate_search_budget(budget, proposals) == original
    usage = dict(model_responses=10, output_tokens=20, wall_seconds=1, artifact_bytes=0)
    assert check_resource_usage(budget, usage)['exhausted'] == ['model_responses']
    with pytest.raises(ValueError, match='计量'):
        check_resource_usage(budget, {})
    path = tmp_path/'budget.json'; path.write_text(json.dumps(dict(plan=budget, proposals=proposals, usage=usage)))
    result = CliRunner().invoke(app, ['validate-search-budget', str(path)])
    assert result.exit_code == 1 and 'may_continue' in result.output
    with pytest.raises(ValueError, match='正整数'):
        validate_search_budget(dict(budget, candidate_capacity=True), proposals)


def test_new_real_registration_requires_bounded_plan_before_creating_artifacts(tmp_path):
    plan, config = make_plan(tmp_path)
    config.write_text(json.dumps(real_plan_data(plan)))
    root = tmp_path/'run'
    with pytest.raises(ValueError, match='bounded-research-v2'):
        register_favor(config, root)
    assert not root.exists()


def test_explicit_unlimited_output_tokens_preserves_other_limits_and_history(tmp_path):
    """关闭 Token 限量不能重置统计历史或关闭其他资源约束。"""
    plan, _ = make_plan(tmp_path)
    budget = bounded(plan.search_budget)
    proposals = [s.model_dump(exclude={'condition_id'}) for s in plan.slots]
    before = validate_search_budget(budget, proposals)
    budget['resource_limits']['output_tokens'] = None
    usage = dict(model_responses=1, output_tokens=10**12, wall_seconds=1, artifact_bytes=0)
    result = check_resource_usage(budget, usage)
    assert result['may_continue'] and result['remaining']['output_tokens'] is None
    assert validate_search_budget(budget, proposals) == before
    path = tmp_path / 'unlimited.json'
    path.write_text(json.dumps(dict(plan=budget, proposals=proposals, usage=usage)))
    assert CliRunner().invoke(app, ['validate-search-budget', str(path)]).exit_code == 0
    assert check_resource_usage(budget, dict(usage, wall_seconds=600))['exhausted'] == ['wall_seconds']
    with pytest.raises(ValueError, match='计量'):
        check_resource_usage(budget, dict(usage, output_tokens=None))
    for key in ('model_responses', 'wall_seconds', 'artifact_bytes'):
        invalid = dict(budget, resource_limits=dict(budget['resource_limits'], **{key: None}))
        with pytest.raises(ValueError, match='正整数'):
            validate_search_budget(invalid, proposals)


def test_impossible_candidate_events_stop_before_label_read_and_keep_slots(tmp_path, monkeypatch):
    plan, config = make_plan(tmp_path)
    plan = plan.model_copy(update={'search_budget': bounded(plan.search_budget),
        'integration': plan.integration.model_copy(update={'min_events_per_ticker': 100000})})
    config.write_text(plan.model_dump_json())
    root = tmp_path/'run'; register_favor(config, root)
    for i, expression in enumerate(expressions(), 1):
        identity = favor_generation_payload(root, f'F{i}', 'gpt6')['submission_identity']
        assert submit_favor(root, dict(identity, expression=expression.model_dump(mode='json')))['status'] == 'compiled'
    original = pl.read_parquet
    def read(path, *args, **kwargs):
        if str(path).endswith('label.parquet'):
            pytest.fail('不可行候选不得打开标签')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(pl, 'read_parquet', read)
    run_favor(root)
    summary = json.loads((root/'summary.json').read_text())
    assert summary['registered'] == 2 and summary['retained_components'] == 0
    assert summary['construct_passed'] == 2 and not summary['return_labels_used']
    assert summary['diagnostic_family_size'] == validate_search_budget(plan.search_budget, [])['diagnostic_family_size']
    from factor_miner.ledger import JsonlLedger
    assert not any(e.outcome_exposed for e in JsonlLedger(root/'ledger').verify())
    from factor_miner.favor_store import projection_payload
    _, kept, trials = projection_payload(root)
    assert not kept and len(trials) == 2


def test_runtime_guard_measures_disk_and_elapsed_without_following_links(tmp_path, monkeypatch):
    from factor_miner.research_pool import ResearchResourceGuard
    import factor_miner.research_pool as pool
    plan, _ = make_plan(tmp_path)
    budget = bounded(plan.search_budget)
    budget['resource_limits']['artifact_bytes'] = 4
    root = tmp_path/'output'; root.mkdir()
    outside = tmp_path/'large'; outside.write_bytes(b'x'*100)
    (root/'input').symlink_to(outside)
    now = [10.]
    monkeypatch.setattr(pool.time, 'monotonic', lambda: now[0])
    guard = ResearchResourceGuard(budget, root)
    guard.check()
    (root/'result').write_bytes(b'1234')
    with pytest.raises(ValueError, match='资源上限'):
        guard.check()
    (root/'result').unlink()
    now[0] += 600
    with pytest.raises(ValueError, match='资源上限'):
        guard.check()


def test_joint_route_cannot_impersonate_generic_incremental_validation(tmp_path):
    from factor_miner.favor_schema import FavorPlan
    plan, _ = make_plan(tmp_path)
    data = plan.model_dump(mode='json')
    data['search_budget'] = dict(bounded(plan.search_budget), validation_target='portfolio_increment')
    with pytest.raises(ValueError, match='联合触发'):
        FavorPlan.model_validate(data)


def test_real_preflight_warning_requires_frozen_explanation_before_registration(tmp_path):
    plan, config = make_plan(tmp_path)
    data = plan.model_dump(mode='json')
    data.update(real_plan_data(plan), search_budget=bounded(plan.search_budget))
    data['integration']['min_events_per_ticker'] = 100000
    config.write_text(json.dumps(data))
    root = tmp_path/'run'
    with pytest.raises(ValueError, match='代理与候选差异'):
        register_favor(config, root)
    assert not root.exists()
    data['search_budget']['feasibility_response'] = '合成测试：代理上限不足仍保留正式候选硬闸门'
    config.write_text(json.dumps(data))
    register_favor(config, root)
    assert json.loads((root/'preflight.json').read_text())['data_feasibility']['needs_review']


def test_preflight_excludes_future_market_values(tmp_path):
    from factor_miner.favor_workflow import file_sha
    plan, _ = make_plan(tmp_path)
    before = inspect_data_feasibility(plan)
    path = Path(plan.dataset_root)/'market.parquet'
    market = pl.read_parquet(path).with_columns(pl.when(pl.col('date') > plan.splits.discovery_end)
        .then(pl.col('close')*1e6).otherwise(pl.col('close')).alias('close'))
    market.write_parquet(path)
    plan = plan.model_copy(update={'input_sha256': dict(plan.input_sha256, **{str(path): file_sha(path)})})
    assert inspect_data_feasibility(plan) == before


def test_exhausted_runtime_preserves_failure_without_completion(tmp_path):
    plan, config = make_plan(tmp_path)
    budget = bounded(plan.search_budget)
    budget['resource_limits']['artifact_bytes'] = 1
    config.write_text(plan.model_copy(update={'search_budget': budget}).model_dump_json())
    root = tmp_path/'run'; register_favor(config, root)
    with pytest.raises(ValueError, match='资源上限'):
        run_favor(root)
    assert (root/'failure.json').exists()
    assert not (root/'completion.json').exists()
