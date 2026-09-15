"""完全合成的 FaVOR 演示发布与事前合同，不使用真实证券或用户数据。"""
from datetime import date, timedelta
import json
from pathlib import Path
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.hypothesis_constraints import ExpressionConstraint, ConditionContract, StateMeasurement
from factor_miner.construct_validation import ObservableCondition
from factor_miner.favor_schema import FavorPlan
from factor_miner.favor_workflow import file_sha

def field(name):
    return FactorNode(op="field", field=name)

def rule(node):
    """测试辅助：把固定模板转成单选约束，不在生产代码反向补写 Γ。"""
    return ExpressionConstraint(operators=(node.op,), arguments=tuple(rule(x) for x in node.args),
        fields=(node.field,) if node.field else (), windows=(node.window,) if node.window else (),
        periods=(node.period,) if node.period is not None else (), constants=(node.value,) if node.value is not None else ())

def expressions():
    intraday = FactorNode(op="sub", args=(FactorNode(op="div", args=(field("close"), field("open"))), FactorNode(op="const", value=1)))
    volume = FactorNode(op="div", args=(field("volume"), FactorNode(op="rolling_mean", args=(field("volume"),), window=20)))
    return intraday, volume

def conditions():
    first, second = expressions()
    return (
        ConditionContract(condition_id="recovery", observation=ObservableCondition(observation="日内回升", measurement="日内收益", response_test="intraday_return"),
            required_fields=("open", "close"), expression_constraint=rule(first),
            state_measurements=(StateMeasurement(name="当期日内回升", expression=first, expected_direction="increase", rationale="以当期收益描述回升，非经济机制证明"),),
            activation_direction="high", expected_return_sign="positive", measurement_rationale="正向比例测量"),
        ConditionContract(condition_id="volume", observation=ObservableCondition(observation="相对放量", measurement="量比", response_test="volume_growth"),
            required_fields=("volume",), expression_constraint=rule(second),
            state_measurements=(StateMeasurement(name="当期相对成交活跃", expression=second, expected_direction="increase", rationale="以当期量比检验状态测量"),),
            activation_direction="high", expected_return_sign="positive", measurement_rationale="当期成交量除20日均量"))

def high_hypothesis():
    from factor_miner.schema import HypothesisSpec
    return HypothesisSpec(claim="日内回升与相对放量同时出现，随后五日可能延续。", mechanism="合成数据测试，不作金融结论。",
        expected_sign="positive", observable_proxy="回升且放量", independent_verification="仅用于工程合成回归",
        competing_explanations=("随机波动",), baseline_reference="合成基准", failure_modes=("状态不持续",),
        falsification_path="固定方向失败即保留失败记录", source_refs=("synthetic_fixture",), mechanism_status="mechanism_unverified")

def make_plan(tmp_path, market_id="us_equity"):
    """行情与标签因果一致的合成发布；不访问用户数据。"""
    from factor_miner.label_dataset import build_fixed_session_o2o_labels
    rng = np.random.default_rng(912)
    days = [date(2018, 1, 1)+timedelta(days=i) for i in range(810)]
    n = 80
    a = np.arange(n)
    t = np.arange(len(days))[:, None]
    recovery = (1+np.sin(t*.13+a*.73))/2
    flow = (1+np.sin(t*.11+a*1.19))/2
    daily = .009*(recovery*flow-.23)+rng.normal(0, .002, size=recovery.shape)
    # T 日状态影响 T+2 的开盘收益，进入固定 T+1/T+6 标签。
    opening = 50*np.cumprod(1+np.vstack([np.zeros((2,n)), daily[:-2]]), axis=0)
    volume = 1e6*np.exp(np.cumsum(.015*(flow-.5), axis=0))
    closing = opening*(1+.025*recovery)
    market = pl.DataFrame(dict(date=np.repeat(np.array(days,dtype="datetime64[D]"),n), asset=np.tile([f"S{i:03d}" for i in a],len(days)),
        open=opening.ravel(), close=closing.ravel(), high=(closing*1.005).ravel(), low=(opening*.995).ravel(), volume=volume.ravel())).with_columns(pl.col("date").cast(pl.Date))
    state = market.select("date", "asset").with_columns(*(pl.lit(True).alias(x) for x in
        ("valid_for_factor_compute", "valid_for_factor_rank", "can_open_long", "can_close_long")))
    dataset = tmp_path/"dataset"; dataset.mkdir()
    market.write_parquet(dataset/"market.parquet")
    state.write_parquet(dataset/"state.parquet")
    pl.DataFrame({"date": days}).write_parquet(dataset/"calendar.parquet")
    build_fixed_session_o2o_labels(market).write_parquet(dataset/"label.parquet")
    contract = dict(market_id=market_id, release_id="synthetic-v1", source="deterministic_fixture", adjustment="split_adjusted_ohlcv",
        calendar_version="synthetic-calendar", state_version="synthetic-state", as_of_date=str(days[-1]), state_as_of_date=str(days[-1]))
    path = dataset/"release.json"; path.write_text(json.dumps(contract))
    budget = dict(candidate_capacity=2, model_capacity=20, historical_attempt_count=0, inherited_diagnostic_family_size=1,
        mechanisms={"joint": dict(capacity=2, information_source="synthetic_fixture", data_ready=True, horizon_rationale="固定5日工程测试")})
    plan = FavorPlan(run_kind="synthetic_demo", market_id=market_id, hypothesis=high_hypothesis(), conditions=conditions(),
        slots=[dict(trial_id=f"F{i+1}", condition_id=c.condition_id, mechanism_id="joint", information_source="synthetic_fixture") for i,c in enumerate(conditions())],
        splits=dict(discovery_start=days[30], discovery_end=days[459], validation_start=days[470], validation_end=days[629], test_start=days[640], test_end=days[799], test_consumed=True),
        integration=dict(validation_thresholds=((.6,.6),(.8,.8)), min_events_per_ticker=2, min_tickers=2, ticker_support_threshold=.05, redundancy_threshold=.75),
        dataset_root=str(dataset), data_contract_path=str(path), input_sha256={str(p):file_sha(p) for p in dataset.iterdir()},
        allowed_fields=("open", "high", "low", "close", "volume"), search_budget=budget, historical_ast_hashes=())
    config = tmp_path/"plan.json"; config.write_text(plan.model_dump_json())
    return plan, config
