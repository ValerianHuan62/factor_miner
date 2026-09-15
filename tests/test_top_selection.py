"""顶部排名只使用当日信息，固定组合不按结果改变方向。"""
from datetime import date
import polars as pl
from factor_miner.top_selection import top_signals, oriented_rank_panel, execute_selected_top


def test_top_labels_follow_frozen_horizon_and_reject_old_protocol_mismatch():
    import pytest
    from factor_miner.top_selection import top_label_column
    assert top_label_column({'version':'top-selection-diagnostic-v2'}, 5) == 'label_o2o_5d'
    assert top_label_column({'version':'top-selection-diagnostic-v3','holding_sessions':20}, 20) == 'label_o2o_20d'
    with pytest.raises(ValueError, match='五日'):
        top_label_column({'version':'top-selection-diagnostic-v2'}, 20)
    with pytest.raises(ValueError, match='不一致'):
        top_label_column({'version':'top-selection-diagnostic-v3','holding_sessions':5}, 20)


def test_top_selection_ties_and_future_nonfill_do_not_change_choice():
    scores=pl.DataFrame({'date':[date(2024,1,2)]*4,'asset':['D','B','A','C'],
        'score':[1.,2.,2.,float('nan')],'future_fill':[True,True,False,True]})
    selected=top_signals(scores,'score',1).filter(pl.col('trigger'))
    assert selected['asset'].to_list()==['A']
    changed=scores.with_columns(~pl.col('future_fill'))
    assert top_signals(scores,'score',1).equals(top_signals(changed,'score',1))


def test_original_negative_direction_and_common_missing_policy():
    panel=pl.DataFrame({'date':[date(2024,1,2)]*3,'asset':['A','B','C'],
        'old':[1.,3.,2.],'new':[2.,1.,None]})
    ranked=oriented_rank_panel(panel,{'old':-1,'new':1},['old'])
    assert ranked['asset'].to_list()==['A','B']
    assert ranked['baseline36'].to_list()==[.75,.25]


def test_selected_price_slice_preserves_nonfill_and_delayed_exit():
    """删去永不持有证券的价格只节省计算，不改变现金、拒单或退出路径。"""
    from tests.test_causal_backtest import inputs, SCHEDULE, SESSIONS
    from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
    factor,market,state=inputs()
    factor=factor.filter(pl.col('security_id').is_in(['A','B']))
    state=state.with_columns(pl.when((pl.col('security_id')=='A')&(pl.col('trade_date')==SESSIONS[1]))
        .then(False).otherwise(pl.col('can_open_long')).alias('can_open_long'),
        pl.when((pl.col('security_id')=='B')&(pl.col('trade_date')==SESSIONS[3]))
        .then(False).otherwise(pl.col('can_close_long')).alias('can_close_long'))
    full=simulate_causal_extreme_portfolio(factor,market,state,SCHEDULE,group_count=1,round_trip_cost_bps=14,
        terminal_policy='report_unresolved',allow_noncontiguous_schedule=True,retain_daily_holdings=False)
    small=execute_selected_top(factor,market,state,SCHEDULE,cost=14,terminal_policy='report_unresolved',terminals=None)
    assert small==full
