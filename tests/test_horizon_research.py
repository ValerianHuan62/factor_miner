"""期限变化保留固定成交端点和缺失语义。"""
from datetime import date,timedelta
import polars as pl
from factor_miner.horizon_research import tradable_fixed_labels


def test_nontradable_endpoint_does_not_roll_to_next_day():
    days=[date(2025,1,1)+timedelta(days=i) for i in range(25)]
    market=pl.DataFrame({'date':days,'asset':['A']*25,'open':[100.+i for i in range(25)]})
    state=market.select('date','asset').with_columns(pl.lit(True).alias('can_open_long'),pl.lit(True).alias('valid_for_factor_rank'),pl.lit(True).alias('valid_for_o2o_label'),(pl.col('date')!=days[2]).alias('can_close_long'))
    x=tradable_fixed_labels(market,state,pl.DataFrame({'date':days}),1)
    assert x['label_exit_date'][0]==days[2] and x['label_o2o_1d'][0] is None
    longer=tradable_fixed_labels(market,state,pl.DataFrame({'date':days}),20)
    assert abs(longer['label_o2o_20d'][0]-(121/101-1))<1e-12
    # 未来不可成交只影响监督标签；所有原信号证券日仍存在。
    assert x.height==market.height==longer.height


def test_available_prices_do_not_override_frozen_label_eligibility():
    days=[date(2025,1,1)+timedelta(days=i) for i in range(10)]
    market=pl.DataFrame({'date':days,'asset':['A']*10,'open':[100.+i for i in range(10)]})
    state=market.select('date','asset').with_columns(pl.lit(True).alias('can_open_long'),pl.lit(True).alias('can_close_long'),pl.lit(True).alias('valid_for_factor_rank'),(pl.col('date')!=days[2]).alias('valid_for_o2o_label'))
    labels=tradable_fixed_labels(market,state,pl.DataFrame({'date':days}),1)
    assert labels['label_o2o_1d'][0] is None
    assert labels['label_o2o_1d'][1] is None
    assert labels['label_o2o_1d'][2] is not None
