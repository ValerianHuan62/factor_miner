"""多期限FaVOR显式版本、固定日期以及月末完整性回归。"""
from datetime import date, timedelta
from pathlib import Path
import json

import polars as pl
import pytest

from factor_miner.favor_demo import make_plan, expressions
from factor_miner.favor_schema import FavorPlan
from factor_miner.favor_workflow import register_favor, submit_favor, run_favor, favor_generation_payload, file_sha
from factor_miner.label_dataset import build_fixed_session_o2o_labels
from factor_miner.research_report import report_schedule


def test_month_end_schedule_does_not_promote_partial_month_and_keeps_fixed_exit():
    days=[date(2025,1,1)+timedelta(days=i) for i in range(150)]
    days=[d for d in days if d.weekday()<5 and d!=date(2025,2,17)]
    schedule=report_schedule(days,date(2025,1,1),date(2025,3,15),'monthly_last_session',holding_sessions=20)
    assert [w.signal_date for w in schedule]==[date(2025,1,31),date(2025,2,28)]
    for w in schedule:
        assert days.index(w.entry_date)==days.index(w.signal_date)+1
        assert days.index(w.exit_date)==days.index(w.signal_date)+21
    assert schedule[1].entry_date < schedule[0].exit_date
    with pytest.raises(ValueError,match='不足'):
        report_schedule(days[:45],days[0],days[44],'monthly_last_session',holding_sessions=20)


def test_legacy_version_cannot_silently_change_horizon(tmp_path):
    plan,_=make_plan(tmp_path)
    data=plan.model_dump(mode='json');data['execution']['holding_sessions']=20
    with pytest.raises(ValueError,match='显式登记'):
        FavorPlan.model_validate(data)


def prepare(tmp_path,horizon,frequency,wrong_dates=False):
    original,_=make_plan(tmp_path)
    data=original.model_dump(mode='json')
    data['version']='favor-gamma-v2'
    data['execution'].update(holding_sessions=horizon,frequency=frequency)
    market=pl.read_parquet(Path(original.dataset_root)/'market.parquet')
    calendar=pl.read_parquet(Path(original.dataset_root)/'calendar.parquet')
    labels=build_fixed_session_o2o_labels(market,calendar=calendar,holding_sessions=5 if wrong_dates else horizon)
    if wrong_dates:labels=labels.rename({'label_o2o_5d':f'label_o2o_{horizon}d'})
    label_path=Path(original.dataset_root)/'label.parquet';labels.write_parquet(label_path)
    data['input_sha256'][str(label_path)]=file_sha(label_path)
    data['splits']['test_end']=str(calendar['date'][779])
    data['hypothesis']['claim']=f'工程合成：日内状态联合条件与随后{horizon}日固定收益。'
    plan=FavorPlan.model_validate(data)
    config=tmp_path/'new_plan.json';config.write_text(plan.model_dump_json())
    root=tmp_path/'new_run';register_favor(config,root)
    for i in range(2):
        identity=favor_generation_payload(root,f'F{i+1}','gpt6')['submission_identity']
        assert submit_favor(root,{**identity,'expression':expressions()[i].model_dump(mode='json')})['status']=='compiled'
    return root


@pytest.mark.parametrize('horizon,frequency',[(1,'daily'),(20,'monthly_last_session')])
def test_version_two_end_to_end_uses_correct_label_and_hac(tmp_path,horizon,frequency):
    root=prepare(tmp_path,horizon,frequency)
    run_favor(root)
    summary=json.loads((root/'summary.json').read_text())
    assert summary['version']=='favor-gamma-v2'
    assert summary['horizon_contract']['label_column']==f'label_o2o_{horizon}d'
    assert summary['horizon_contract']['frequency']==frequency
    assert summary['horizon_contract']['ic_hac_max_lags']==max(5,horizon)
    for result in summary['factors'].values():
        assert result['discovery']['hac_max_lags']==max(5,horizon)
    assert (root/'benchmark.json').exists()
    assert (root/'completion.json').exists()


def test_renamed_five_day_labels_cannot_impersonate_month_horizon(tmp_path):
    root=prepare(tmp_path,20,'monthly_last_session',wrong_dates=True)
    with pytest.raises(ValueError,match=r'T\+1/T\+21'):
        run_favor(root)


@pytest.mark.parametrize('label_column', ['label_o2o_1d', 'label_o2o_20d'])
def test_new_targets_cannot_enter_formula_or_construct_measurement(label_column):
    """即使输入白名单误含新标签，候选和构念测量也必须拒绝它。"""
    from factor_miner.compiler import compile_candidate
    from factor_miner.errors import FactorMinerError
    from factor_miner.favor_demo import conditions, high_hypothesis, field
    from factor_miner.hypothesis_constraints import compile_gamma
    from tests.helpers import valid_candidate

    expression = field(label_column)
    candidate = valid_candidate().model_copy(update={
        'expression': expression, 'required_fields': (label_column,), 'max_lookback': 0})
    with pytest.raises(FactorMinerError, match='禁止引用字段'):
        compile_candidate(candidate, (label_column,))
    condition = conditions()[0]
    allowed = ('open', 'close', label_column)
    gamma = compile_gamma(high_hypothesis(), condition, allowed)
    with pytest.raises(FactorMinerError, match='禁止引用字段'):
        gamma.validate(expression, high_hypothesis())
    contaminated = condition.model_copy(update={
        'state_measurements': (condition.state_measurements[0].model_copy(
            update={'expression': expression}),)})
    with pytest.raises(FactorMinerError, match='禁止引用字段'):
        compile_gamma(high_hypothesis(), contaminated, allowed)
