"""市场上下文必须身份冻结、同日连接并完整覆盖日历。"""
from datetime import date,timedelta
from pathlib import Path
import hashlib,json,tempfile,unittest
import polars as pl
from factor_miner.market_context import attach_market_context
from factor_miner.construct_validation import ObservableCondition,validate_construct


class MarketContextTest(unittest.TestCase):
    def test_curvature_response_is_distinct_from_unrelated_volume(self):
        close={'op':'field','field':'close'};market={'op':'field','field':'market_return'}
        expression={'op':'rolling_corr','args':[{'op':'div','args':[close,{'op':'calendar_delay','args':[close],'period':1}]},
                    {'op':'mul','args':[market,market]}],'window':120}
        condition=ObservableCondition(observation='市场极端幅度响应',measurement='收益与市场平方的相关',response_test='market_curvature')
        self.assertEqual(validate_construct(expression,condition)['status'],'基础检验符合')
        wrong={'op':'div','args':[{'op':'field','field':'volume'},{'op':'rolling_mean','args':[{'op':'field','field':'volume'}],'window':20}]}
        self.assertEqual(validate_construct(wrong,condition)['status'],'偏离')

    def test_exact_date_join_and_fail_closed_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cal=root/'calendar.parquet';ctx=root/'context.parquet';contract=root/'contract.json'
            dates=[date(2020,1,1)+timedelta(days=i) for i in range(3)]
            pl.DataFrame({'date':dates}).write_parquet(cal)
            data=pl.DataFrame({'date':dates,'market_return':[.01,-.02,.03]});data.write_parquet(ctx)
            frozen=dict(version='market-context-v1',field='market_return',observation='close_t',availability='after_close_t',
                return_definition='close_t/previous_market_close-1',price_basis='price_return_excluding_dividends',
                data_sha256=hashlib.sha256(ctx.read_bytes()).hexdigest(),calendar_sha256=hashlib.sha256(cal.read_bytes()).hexdigest())
            contract.write_text(json.dumps(frozen))
            market=pl.DataFrame({'date':[dates[2],dates[0]],'asset':['A','A'],'close':[12.,10.]}).lazy()
            actual=attach_market_context(market,cal,ctx,contract).collect().sort('date')
            self.assertEqual(actual['market_return'].to_list(),[.01,.03]);self.assertEqual(actual.height,2)
            for invalid in [data.head(2),pl.concat([data,data.head(1)]),data.with_columns(pl.lit(float('nan')).alias('market_return'))]:
                invalid.write_parquet(ctx)
                with self.assertRaises(ValueError):attach_market_context(market,cal,ctx,contract)
                refreshed={**frozen,'data_sha256':hashlib.sha256(ctx.read_bytes()).hexdigest()}
                contract.write_text(json.dumps(refreshed))
                with self.assertRaises(ValueError):attach_market_context(market,cal,ctx,contract)
                contract.write_text(json.dumps(frozen))

    def test_construct_distinguishes_market_response_from_volume(self):
        close={'op':'field','field':'close'}
        expression={'op':'rolling_corr','args':[{'op':'div','args':[close,{'op':'calendar_delay','args':[close],'period':1}]},
                    {'op':'field','field':'market_return'}],'window':120}
        condition=ObservableCondition(observation='市场相关性',measurement='同期Pearson相关',response_test='market_correlation')
        self.assertEqual(validate_construct(expression,condition)['status'],'基础检验符合')
        wrong={'op':'div','args':[{'op':'field','field':'volume'},{'op':'rolling_mean','args':[{'op':'field','field':'volume'}],'window':20}]}
        self.assertEqual(validate_construct(wrong,condition)['status'],'偏离')
