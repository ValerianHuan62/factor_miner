"""年报修订只能影响公告之后，年度比较必须使用当时已知版本。"""
from datetime import date
import polars as pl
import pytest
from factor_miner.pit_financials import annual_snapshots, align_annual_panel
from factor_miner.construct_validation import ObservableCondition, validate_construct


def test_revision_cannot_backfill_or_replace_newer_report():
    days = [date(2021, 4, d) for d in (1, 2, 5, 6, 7, 8)]
    rows = [('2019q4', days[0], 10.), ('2020q4', days[1], 30.), ('2019q4', days[3], 20.)]
    versions = pl.DataFrame([dict(order_book_id='A', quarter=q, info_date=d, gross_profit=g,
        cash_flow_from_operating_activities=5., total_assets=100., total_liabilities=40.) for q,d,g in rows])
    snapshots = annual_snapshots(versions, pl.DataFrame({'date':days}), days[-1])
    panel = align_annual_panel(pl.DataFrame({'date':days,'asset':['A']*len(days)}), snapshots, 550)
    assert panel['annual_gross_profit'].to_list() == [None,10.,30.,30.,30.,30.]
    assert panel['prior_annual_gross_profit'].to_list() == [None,None,10.,10.,20.,20.]
    with pytest.raises(ValueError, match='冲突'):
        annual_snapshots(pl.concat([versions, versions.head(1).with_columns(pl.lit(99.).alias('gross_profit'))]),
            pl.DataFrame({'date':days}), days[-1])


def test_stale_report_and_invalid_denominator_remain_missing():
    versions = pl.DataFrame([dict(order_book_id='A', quarter='2019q4',info_date=date(2020,4,1),
        gross_profit=10.,cash_flow_from_operating_activities=5.,total_assets=0.,total_liabilities=40.)])
    days=[date(2020,4,1),date(2020,4,2),date(2022,1,1)]
    snapshots=annual_snapshots(versions,pl.DataFrame({'date':days}),days[-1])
    panel=align_annual_panel(pl.DataFrame({'date':days,'asset':['A']*3}),snapshots,550)
    assert panel['annual_total_assets'].null_count()==3
    assert panel['annual_gross_profit'].to_list()==[None,10.,None]


@pytest.mark.parametrize('field,response',[('annual_gross_profit','annual_gross_profit'),
    ('annual_cash_flow_from_operating_activities','annual_cash_flow'),('annual_total_liabilities','annual_leverage')])
def test_financial_construct_rejects_inverted_response(field,response):
    expression={'op':'div','args':[{'op':'field','field':field},{'op':'field','field':'annual_total_assets'}]}
    condition=ObservableCondition(observation='固定资产时分子增加',measurement='年报比率',response_test=response)
    assert validate_construct(expression,condition)['status']=='基础检验符合'
    assert validate_construct(expression,condition.model_copy(update={'expected_response':'decrease'}))['status']=='偏离'


def test_profit_improvement_uses_prior_year_and_passes_response():
    expression={'op':'sub','args':[{'op':'div','args':[{'op':'field','field':prefix+'gross_profit'},
        {'op':'field','field':prefix+'total_assets'}]} for prefix in ('annual_','prior_annual_')]}
    condition=ObservableCondition(observation='当期毛利润提高而上年不变',measurement='年度盈利改善',response_test='annual_profit_improvement')
    assert validate_construct(expression,condition)['status']=='基础检验符合'
