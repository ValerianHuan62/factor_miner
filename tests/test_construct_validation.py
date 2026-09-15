"""用经济尺度与反例检验构念审核，而不是重复描述实现。"""

from factor_miner.construct_validation import ObservableCondition, validate_construct


def test_price_amount_cannot_pass_return_measurement_contract() -> None:
    """股价变化金额受股份单位变化影响，收益率不应受影响。"""
    close = {"op": "field", "field": "close"}
    delta = {"op": "delta", "args": [close], "period": 1}
    returns = {"op": "div", "args": [delta, {"op": "delay", "args": [close], "period": 1}]}
    condition = ObservableCondition(observation="价格趋势", measurement="收益率", response_test="trend")
    assert validate_construct(delta, condition)["status"] == "偏离"
    assert validate_construct(returns, condition)["status"] == "基础检验符合"
    assert validate_construct(returns, None)["status"] == "证据不足"


def test_absolute_volume_coupling_distinguishes_direction_and_measurement() -> None:
    """绝对收益与成交活跃联动不应被反向公式解释为同一测量。"""
    close = {"op": "field", "field": "close"}
    volume = {"op": "field", "field": "volume"}
    returns = {"op": "div", "args": [{"op": "delta", "args": [close], "period": 1},
                                       {"op": "delay", "args": [close], "period": 1}]}
    corr = {"op": "rolling_corr", "args": [{"op": "abs", "args": [returns]}, volume], "window": 20}
    condition = ObservableCondition(observation="交易量与绝对收益的同期联动", measurement="相关系数",
                                    response_test="absolute_return_volume_coupling")
    assert validate_construct(corr, condition)["status"] == "基础检验符合"
    assert validate_construct(corr, condition.model_copy(update={"expected_response": "decrease"}))["status"] == "偏离"


def test_range_persistence_detects_temporal_order() -> None:
    """相同振幅分布重新排序后，序列相关指标应识别持续性。"""
    ratio = {"op": "div", "args": [{"op": "field", "field": "high"}, {"op": "field", "field": "low"}]}
    corr = {"op": "rolling_corr", "args": [ratio, {"op": "delay", "args": [ratio], "period": 1}], "window": 20}
    condition = ObservableCondition(observation="每日振幅的时间持续性", measurement="一阶相关系数", response_test="range_persistence")
    assert validate_construct(corr, condition)["status"] == "基础检验符合"
