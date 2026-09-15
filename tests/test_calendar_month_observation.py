"""日历月观察的精确端点、显式版本、构念及冻结流程回归。"""
from datetime import date, timedelta
import json
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from factor_miner.compiler import attach_market_sessions, build_polars_expr, compile_candidate
from factor_miner.construct_validation import ObservableCondition, validate_construct
from factor_miner.dsl import CALENDAR_MONTH_LIMITS, canonical_ast, validate_ast
from factor_miner.errors import FactorMinerError
from factor_miner.favor_demo import make_plan, high_hypothesis
from factor_miner.favor_schema import FavorPlan
from factor_miner.favor_workflow import register_favor, submit_favor, favor_generation_payload, load_plan, run_favor
from factor_miner.hypothesis_constraints import ConditionContract, ExpressionConstraint, StateMeasurement, compile_gamma
from factor_miner.schema import FactorNode, TrustedCandidateFactorSpec


def delayed(period, field='close'):
    return FactorNode(op='calendar_month_delay', period=period, args=(FactorNode(op='field', field=field),))


def ratio(field, newer, older):
    return FactorNode(op='div', args=(delayed(newer, field), delayed(older, field)))


def test_exact_month_end_missing_rows_leap_holiday_and_future_invariance():
    days = [date(2022, 1, 1) + timedelta(days=i) for i in range(1000)]
    days = [d for d in days if d.weekday() < 5 and d != date(2024, 3, 29)]
    frame = pl.DataFrame({'date': days * 2, 'asset': ['A'] * len(days) + ['B'] * len(days), 'close': list(range(len(days))) * 2})
    frame = frame.filter(~((pl.col('asset') == 'B') & (pl.col('date') == date(2024, 2, 29))))
    calendar = pl.DataFrame({'date': days})

    def evaluate(f):
        return attach_market_sessions(f.lazy(), calendar, calendar_months=True).sort('asset', 'date').with_columns(
            [build_polars_expr(delayed(m)).alias(f'm{m}') for m in (1, 6, 7, 18)]).collect()

    result = evaluate(frame)
    for asset in ('A', 'B'):
        for signal in (date(2024, 3, 1), date(2024, 4, 1), date(2024, 1, 2)):
            row = result.filter((pl.col('asset') == asset) & (pl.col('date') == signal)).row(0, named=True)
            for months in (1, 6, 7, 18):
                target_month = signal.year * 12 + signal.month - 1 - months
                target = max(d for d in days if d.year * 12 + d.month - 1 == target_month)
                expected = None if asset == 'B' and target == date(2024, 2, 29) else days.index(target)
                assert row[f'm{months}'] == expected
    assert result.filter(pl.col('date') < date(2022, 2, 1))['m1'].null_count() == 2 * sum(d < date(2022, 2, 1) for d in days)
    mutated = frame.with_columns(pl.when(pl.col('date') > date(2024, 4, 1)).then(-999).otherwise(pl.col('close')).alias('close'))
    before = result.filter(pl.col('date') <= date(2024, 4, 1))
    assert before.equals(evaluate(mutated).filter(pl.col('date') <= date(2024, 4, 1)))
    with pytest.raises(ValueError, match='保留'):
        attach_market_sessions(frame.with_columns(pl.lit(0).alias('__month_session_1')).lazy(), calendar, calendar_months=True)


def test_new_clock_cannot_relax_legacy_windows_depth_or_nested_temporal():
    for p in (1, 6, 7, 18):
        with pytest.raises(FactorMinerError, match='显式协议'):
            validate_ast(delayed(p), ('close',), ())
        metadata = validate_ast(delayed(p), ('close',), (), CALENDAR_MONTH_LIMITS)
        assert metadata.lookback == 31 * p and metadata.depth == 2
        assert canonical_ast(delayed(p))['period'] == p
    for p in (0, 2, 12, 19, -1):
        with pytest.raises(FactorMinerError):
            validate_ast(delayed(p), ('close',), (), CALENDAR_MONTH_LIMITS)
    for node in (FactorNode(op='delay', period=500, args=(FactorNode(op='field', field='close'),)),
                 FactorNode(op='rolling_mean', window=20, args=(delayed(1),)),
                 FactorNode(op='calendar_month_delay', period=6, args=(delayed(1),))):
        with pytest.raises(FactorMinerError):
            validate_ast(node, ('close',), (), CALENDAR_MONTH_LIMITS)
    node = delayed(18)
    for _ in range(4):
        node = FactorNode(op='neg', args=(node,))
    with pytest.raises(FactorMinerError, match='深度'):
        validate_ast(node, ('close',), (), CALENDAR_MONTH_LIMITS)


def rule(node):
    return ExpressionConstraint(operators=(node.op,), fields=(node.field,) if node.field else (),
        periods=(node.period,) if node.period is not None else (), arguments=tuple(rule(c) for c in node.args))


def condition(field, newer, older, response):
    expression = ratio(field, newer, older)
    observation = ObservableCondition(observation='工程合成状态', measurement='精确日历端点相对变化',
        response_test=response, expected_response='decrease' if response == 'monthly_share_change' else 'increase')
    contract = ConditionContract(condition_id=response, observation=observation, required_fields=(field,),
        expression_constraint=rule(expression), activation_direction='low' if response == 'monthly_share_change' else 'high',
        expected_return_sign='negative' if response == 'monthly_share_change' else 'positive', measurement_rationale='工程合成，不作金融结论',
        state_measurements=(StateMeasurement(name='synthetic_state', expression=expression, expected_direction='increase', rationale='工程反例状态'),))
    return contract, expression


@pytest.mark.parametrize('field,newer,older,response', [('event_adjusted_shares', 6, 18, 'monthly_share_change'), ('close', 1, 7, 'monthly_price_repair')])
def test_monthly_construct_has_response_unit_and_observation_lag_tests(field, newer, older, response):
    c, expression = condition(field, newer, older, response)
    result = validate_construct(expression, c.observation, calendar_months=True)
    assert result['status'] == '基础检验符合', result
    assert len(result['checks']) == 3 and not result['return_labels_used']
    reversed_expression = FactorNode(op='neg', args=(expression,))
    assert validate_construct(reversed_expression, c.observation, calendar_months=True)['status'] == '偏离'
    with pytest.raises(FactorMinerError):
        compile_gamma(high_hypothesis(), c, (field,))
    gamma = compile_gamma(high_hypothesis(), c, (field,), version='hypothesis-gamma-calendar-month-v1')
    gamma.validate(expression, high_hypothesis())


def test_version_three_freezes_submits_compiles_and_executes_same_workflow(tmp_path):
    plan, _ = make_plan(tmp_path)
    old = plan.model_dump_json()
    assert FavorPlan.model_validate_json(old).model_dump_json() == old
    c, expression = condition('close', 1, 7, 'monthly_price_repair')
    data = plan.model_dump(mode='json')
    data['version'] = 'favor-gamma-v3'
    data['conditions'][0] = c.model_dump(mode='json')
    data['slots'][0]['condition_id'] = c.condition_id
    plan = FavorPlan.model_validate(data)
    from factor_miner.favor_preflight import inspect_measurement_contracts
    assert inspect_measurement_contracts(plan)['passed']
    path = tmp_path/'monthly_plan.json'; path.write_text(plan.model_dump_json())
    root = tmp_path/'monthly_run'; register_favor(path, root)
    assert load_plan(root).model_dump_json() == plan.model_dump_json()
    identity = favor_generation_payload(root, 'F1', 'gpt6')['submission_identity']
    receipt = submit_favor(root, {**identity, 'expression': expression.model_dump(mode='json')})
    assert receipt['status'] == 'compiled', receipt
    spec = TrustedCandidateFactorSpec.model_validate_json((root/'submissions/F1/spec.json').read_text())
    assert spec.spec_version == '3' and spec.max_lookback == 217
    compiled = compile_candidate(spec, plan.allowed_fields)
    assert compiled.expression_metadata['required_context'] == 'explicit_market_calendar_month_v1'
    from typer.testing import CliRunner
    from factor_miner.cli import app
    cli = CliRunner().invoke(app, ['compile-spec', str(root/'submissions/F1/spec.json')])
    assert cli.exit_code == 0, cli.output
    legacy_data = spec.model_dump(mode='json'); legacy_data['spec_version'] = '2'
    with pytest.raises(ValueError, match='130'):
        TrustedCandidateFactorSpec.model_validate(legacy_data)
    with pytest.raises(ValueError, match='协议不一致'):
        compile_candidate(spec.model_copy(update={'spec_version': '2', 'max_lookback': 130}), plan.allowed_fields)
    run_favor(root)
    summary = json.loads((root/'summary.json').read_text())
    assert summary['status'] == 'completed'
    assert summary['factors']['F1']['synthetic']['status'] == '基础检验符合'
    assert (root/'completion.json').exists()
