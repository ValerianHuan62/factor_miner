"""绝对已结算收益和未确定基准的相对绩效分开。"""
from factor_miner.weekly_target_study import actual_portfolio_metrics

def test_resolved_portfolio_does_not_certify_reference_benchmark():
    rows=[dict(entry_date='2025-01-01',exit_date='2025-01-02',target_long_net_return=.01,benchmark_return=.002,target_long_turnover=.2,target_long_cost=.001),dict(entry_date='2025-01-02',exit_date='2025-01-03',target_long_net_return=-.005,benchmark_return=.001,target_long_turnover=0.,target_long_cost=0.)]
    result=actual_portfolio_metrics(rows,False)
    assert result['period_return']>0
    assert result['information_ratio'] is None
    assert result['benchmark_period_return'] is None
    assert result['annualized_return_minus_benchmark'] is None
    assert actual_portfolio_metrics(rows,True) is None
