"""统一 Γ/FaVOR 的反例、未来污染、预算及完整合成工作流回归。"""
from datetime import date, timedelta
import json
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from factor_miner.schema import FactorNode, ExpectedSign
from factor_miner.hypothesis_constraints import ExpressionConstraint, ConditionContract, StateMeasurement, compile_gamma
from factor_miner.construct_validation import ObservableCondition
from factor_miner.favor_validation import validate_empirical_construct, ConstructPolicy
from factor_miner.favor_integration import joint_signals, percentile_scores, directional_selectivity
from factor_miner.favor_schema import FavorPlan
from factor_miner.favor_workflow import register_favor, submit_favor, run_favor, favor_generation_payload, file_sha
from factor_miner.favor_demo import field, expressions, conditions, high_hypothesis, make_plan


def test_gamma_rejects_changed_window_missing_variable_negation_and_hypothesis():
    c = conditions()[1]
    h = high_hypothesis()
    gamma = compile_gamma(h, c, ("open", "close", "volume"))
    original = expressions()[1]
    gamma.validate(original,h)
    wrong = original.model_copy(update={"args": (original.args[0], original.args[1].model_copy(update={"window":40}))})
    for expression in (wrong, FactorNode(op="neg", args=(original,)), field("close"), FactorNode(op="div", args=(field("volume"),field("volume")))):
        with pytest.raises(ValueError):
            gamma.validate(expression,h)
    with pytest.raises(ValueError,match="原假设"):
        gamma.validate(original,h.model_copy(update={"expected_sign":ExpectedSign.NEGATIVE}))


def test_joint_is_and_nested_missing_safe_and_independent_of_labels():
    day=date(2020,1,1)
    a=pl.DataFrame(dict(date=[day]*4,asset=list("ABCD"),score=[.95,.95,.6,.2]))
    b=a.with_columns(pl.Series("score",[.95,.2,.95,.2]))
    scores={"first":a,"second":b}
    signal=joint_signals(scores,("first","second"),(.9,.9))
    assert signal.filter(pl.col("trigger"))["asset"].to_list()==["A"]
    assert joint_signals(scores,("first","second"),(.5,.5)).filter(pl.col("trigger")).height==2
    assert "label" not in str(signal.columns)
    scores["second"]=b.filter(pl.col("asset")!="A")
    assert not joint_signals(scores,("first","second"),(.9,.9))["trigger"].any()


def test_selectivity_purges_future_exit_and_counts_insufficient_tickers():
    start=date(2020,1,1); boundary=start+timedelta(days=100)
    rows=[dict(date=start+timedelta(days=i),asset="A",label_o2o_5d=float(i)/100-.2,label_exit_date=start+timedelta(days=i+6)) for i in range(90)]
    labels=pl.DataFrame(rows)
    ladder={q:labels.select("date","asset",(pl.col("label_o2o_5d")>q-.5).alias("trigger")) for q in (.5,.7,.9)}
    kwargs=dict(start=start,end=boundary-timedelta(days=1),boundary=boundary,min_events=2,min_tickers=1,support_threshold=.5)
    baseline=directional_selectivity(ladder,labels,**kwargs)
    assert baseline["passed"]
    leaked=labels.with_columns(pl.lit(boundary).alias("label_exit_date"))
    rejected=directional_selectivity(ladder,leaked,**kwargs)
    assert not rejected["passed"]
    assert rejected["tickers"]==1


def test_constant_factor_cannot_pass_five_bins(tmp_path):
    plan,_=make_plan(tmp_path)
    market=pl.read_parquet(Path(plan.dataset_root)/"market.parquet")
    state=pl.read_parquet(Path(plan.dataset_root)/"state.parquet")
    raw=market.select("date","asset",pl.lit(1.).alias("raw_factor"),pl.lit(True).alias("valid_for_factor_compute"))
    result=validate_empirical_construct(raw,market,state,conditions()[0],plan.splits.discovery_start,plan.splits.discovery_end,ConstructPolicy())
    assert not result["passed"]
    assert len(result["measurements"][0]["profile"])==1


def test_frozen_budget_rejects_combination_explosion(tmp_path):
    plan,_=make_plan(tmp_path)
    data=plan.model_dump(mode="json")
    data["search_budget"]["model_capacity"]=1
    with pytest.raises(ValueError,match="模型名额"):
        FavorPlan.model_validate(data)


def test_submission_failure_consumes_slot_and_cannot_be_replaced(tmp_path):
    _,config=make_plan(tmp_path)
    root=tmp_path/"run"; register_favor(config,root)
    identity=favor_generation_payload(root,"F1","gpt6")["submission_identity"]
    bad={**identity,"expression":field("volume").model_dump(mode="json")}
    assert submit_favor(root,bad)["status"]=="compile_failed"
    with pytest.raises(FileExistsError):
        submit_favor(root,{**identity,"expression":expressions()[0].model_dump(mode="json")})
    from factor_miner.ledger import JsonlLedger
    assert any(e.event_type.value=="compile_failed" for e in JsonlLedger(root/"ledger").verify())


@pytest.mark.parametrize("market_id", ["a_share", "us_equity"])
def test_complete_pipeline_both_producers_and_markets(tmp_path,market_id):
    plan,config=make_plan(tmp_path,market_id)
    root=tmp_path/"run"; register_favor(config,root)
    for i,producer in enumerate(("deepseek_api","gpt6")):
        identity=favor_generation_payload(root,f"F{i+1}",producer)["submission_identity"]
        receipt=submit_favor(root,{**identity,"expression":expressions()[i].model_dump(mode="json")})
        assert receipt["status"]=="compiled",receipt
    run_favor(root)
    summary=json.loads((root/"summary.json").read_text())
    assert summary["status"]=="completed"
    assert summary["construct_passed"]==2, summary["factors"]
    assert summary["retained_combinations"]==1, summary["combinations"]
    assert summary["retained_components"]==2
    assert not summary["sealed_oos"]
    assert (root/"strategies/combo_001/backtest.json").exists()
    assert not json.loads((root/"strategy_freeze.json").read_text())["test_used_for_selection"]
    from factor_miner.favor_store import projection_payload
    projected,kept,trials=projection_payload(root)
    assert len(kept)==2 and len(trials)==2
    assert all(item["report"]["portfolio"]["win_rate"] is not None for item in kept)
    with pytest.raises(ValueError,match="开始评价"):
        submit_favor(root,{"trial_id":"F1"})


def test_empty_joint_signal_keeps_cash_and_default_stays_strict():
    from tests.test_causal_backtest import inputs,SCHEDULE
    from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
    from factor_miner.errors import FactorMinerError
    factor,market,state=inputs()
    with pytest.raises(FactorMinerError):
        simulate_causal_extreme_portfolio(factor.head(0),market,state,SCHEDULE,group_count=1)
    result=simulate_causal_extreme_portfolio(factor.head(0),market,state,SCHEDULE,group_count=1,allow_empty_signals=True)
    assert not result.orders and result.execution_summary["final_cash"]==1.


def test_deepseek_request_contains_contract_not_prices(tmp_path):
    from factor_miner.favor_workflow import prepare_favor_deepseek
    _,config=make_plan(tmp_path)
    root=tmp_path/"run";register_favor(config,root)
    output=tmp_path/"request.json";prepare_favor_deepseek(root,"F1",output)
    text=output.read_text()
    assert "expression_constraint" in text and "gamma_sha256" in text
    assert "S000" not in text and "dataset_root" not in text and "test_ic" not in text


def test_a_share_llm_approval_cannot_override_gamma():
    from tests.test_llm_candidate import hypothesis,price_registry,NOW
    from factor_miner.field_registry import FieldAvailabilityRegistry
    from factor_miner.llm_candidate import CandidateExpressionDraft,SemanticLintDecision,convert_candidate
    from factor_miner.llm_hypothesis import PredictionProposal,compose_testable_prediction,registered_coverage_gap_hypothesis
    from factor_miner.policy import company_a_share_visible_policy
    base=hypothesis(); condition=conditions()[0]
    proposal=PredictionProposal(observable_proxy=condition.observation.observation,expected_sign="positive",
        proposed_field_aliases=("price_close","price_open"),proposed_operator_families=("arithmetic",),
        observable_condition=condition.observation,measurement_contract=condition)
    draft=base.draft.model_copy(update={"prediction_proposal":proposal})
    approved=registered_coverage_gap_hypothesis(draft,compose_testable_prediction(proposal,company_a_share_visible_policy(),
        discovery_family_id="llmfamily_"+"1"*24),base.decision)
    close=price_registry().fields[0]
    registry=FieldAvailabilityRegistry(registry_id="gamma-fixture",data_release_id="synthetic",
        fields=(close,close.model_copy(update={"field_id":"open","public_alias":"price_open"})))
    expression=FactorNode(op="sub",args=(FactorNode(op="div",args=(field("price_close"),field("price_open"))),FactorNode(op="const",value=1)))
    lint=SemanticLintDecision(candidate_slot_id="coverage_outcome_llm:C001",decision="approved",proxy_alignment=True,
        direction_alignment=True,availability_alignment=True,undeclared_exposure=False,reason_codes=(),summary="模拟错误的 LLM 批准")
    def convert(node):
        return convert_candidate(draft=CandidateExpressionDraft(candidate_slot_id=lint.candidate_slot_id,expression=node),
            hypothesis=approved,lint=lint,registry=registry,created_at=NOW,provenance={"audit":"synthetic"})
    assert convert(expression).provenance["gamma_sha256"]
    with pytest.raises(ValueError,match="Γ"):
        convert(FactorNode(op="neg",args=(expression,)))


def test_observation_mapping_rejects_reversed_state_and_ignores_future(tmp_path):
    from factor_miner.compiler import build_polars_expr
    plan,_=make_plan(tmp_path)
    market=pl.read_parquet(Path(plan.dataset_root)/"market.parquet")
    state=pl.read_parquet(Path(plan.dataset_root)/"state.parquet")
    raw=market.select("date","asset",build_polars_expr(expressions()[0]).alias("raw_factor"),pl.lit(True).alias("valid_for_factor_compute"))
    c=conditions()[0]
    args=(raw,market,state,c,plan.splits.discovery_start,plan.splits.discovery_end,ConstructPolicy())
    result=validate_empirical_construct(*args)
    assert result["passed"]
    changed=market.with_columns(pl.when(pl.col("date")>plan.splits.discovery_end).then(pl.col("close")*100).otherwise(pl.col("close")).alias("close"))
    assert validate_empirical_construct(raw,changed,state,c,*args[4:])==result
    reversed_condition=c.model_copy(update={"state_measurements":tuple(m.model_copy(update={"expected_direction":"decrease"}) for m in c.state_measurements)})
    assert not validate_empirical_construct(raw,market,state,reversed_condition,*args[4:])["passed"]


def test_test_labels_cannot_change_threshold_selection(tmp_path):
    from factor_miner.favor_workflow import file_sha
    plan,config=make_plan(tmp_path)
    def execute(directory):
        register_favor(config,directory)
        for i in range(2):
            identity=favor_generation_payload(directory,f"F{i+1}","gpt6")["submission_identity"]
            submit_favor(directory,{**identity,"expression":expressions()[i].model_dump(mode="json")})
        run_favor(directory)
        summary=json.loads((directory/"summary.json").read_text())
        return summary["combinations"]["combo_001"]["thresholds"]
    before=execute(tmp_path/"original")
    path=Path(plan.dataset_root)/"label.parquet"
    frame=pl.read_parquet(path).with_columns(pl.when(pl.col("date")>=plan.splits.test_start)
        .then(-pl.col("label_o2o_5d")*100).otherwise(pl.col("label_o2o_5d")).alias("label_o2o_5d"))
    frame.write_parquet(path)
    data=plan.model_dump(mode="json");data["input_sha256"][str(path)]=file_sha(path)
    config.write_text(json.dumps(data))
    assert execute(tmp_path/"changed_test")==before
