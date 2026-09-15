"""简单策略只能用发现期规则筛选，不得受到确认收益影响。"""

from copy import deepcopy
from factor_miner.simple_strategy import select_strategy_factors


def test_selection_ignores_confirmation_and_keeps_thresholds():
    """确认期字段变化不改变所选因子，构念偏离不能因高收益放行。"""
    report = {"slot": 1, "hypothesis_id": "H01", "factor_id": "huan004", "direction": "positive", "hypothesis_direction": "positive",
        "construct_validation": {"status": "基础检验符合"},
        "discovery": {"bonferroni_p_value": .001, "rank_ic_mean": .03, "median_coverage": .9, "rank_ic_hac_t": 5.}, "evaluation": {"rank_ic_mean": -.8}}
    changed = deepcopy(report)
    changed["evaluation"]["rank_ic_mean"] = .8
    assert [item["factor_id"] for item in select_strategy_factors([report], set())] == [item["factor_id"] for item in select_strategy_factors([changed], set())]
    changed["construct_validation"]["status"] = "偏离"
    assert not select_strategy_factors([changed], set())
    assert not select_strategy_factors([report], {0})
