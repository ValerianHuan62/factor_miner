"""VIX必须按原CSV与股票日历计算点数变化，且不能偷用16:00之后数据成交。"""
from datetime import date,timedelta
from pathlib import Path
import json,hashlib,tempfile,unittest
import polars as pl
from factor_miner.vix_context import attach_ovx_context

class OvxContextTest(unittest.TestCase):
    def test_source_derivation_calendar_missing_and_availability(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.csv';calendar=root/'calendar.parquet';context=root/'context.parquet';contract=root/'contract.json'
            days=[date(2020,1,1)+timedelta(days=i) for i in range(5)]
            # 股票日历跳过第3日，变化必须使用上一股票日，而不是上一CSV观察日。
            pl.DataFrame(dict(date=[days[1],days[3]])).write_parquet(calendar)
            data=pl.DataFrame(dict(DATE=[x.strftime('%m/%d/%Y') for x in days],OVX=[20.,22.,30.,25.,29.]))
            data.write_csv(source);valid_context=pl.DataFrame(dict(date=[days[1],days[3]],ovx_change=[2.,3.]));valid_context.write_parquet(context)
            frozen=dict(version='ovx-context-v1',field='ovx_change',observation='index_close_t',availability='after_required_index_sessions_finalized_and_complete_vendor_delivery',earliest_trade='open_t_plus_1',
                definition='ovx_close_t-minus-ovx_close_previous_stock_market_session',unit='volatility_index_points',revision_policy='historical_download_no_vintage_guarantee',
                source_snapshot_path=str(source),previous_session_date=str(days[0]),missing_policy="preregistered_source_gaps_propagate_null_without_fill",source_missing_dates=[])
            def write_contract(**changes):
                active={**frozen,'data_sha256':hashlib.sha256(context.read_bytes()).hexdigest(),'calendar_sha256':hashlib.sha256(calendar.read_bytes()).hexdigest(),
                    'source_snapshot_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),**changes}
                contract.write_text(json.dumps(active))
            frame=pl.DataFrame(dict(date=[days[3],days[1]],asset=['A','A'],close=[12.,10.])).lazy()
            write_contract();actual=attach_ovx_context(frame,calendar,context,contract).collect().sort('date')
            self.assertEqual(actual['ovx_change'].to_list(),[2.,3.]);self.assertEqual(actual.height,2)
            for invalid in [valid_context.head(1),pl.concat([valid_context,valid_context.head(1)]),valid_context.with_columns(pl.lit(float('nan')).alias('ovx_change')),
                            valid_context.with_columns(pl.lit(0.).alias('ovx_change'))]:
                invalid.write_parquet(context);write_contract()
                with self.assertRaises(ValueError):attach_ovx_context(frame,calendar,context,contract)
            valid_context.write_parquet(context);write_contract(availability='at_16_00_et')
            with self.assertRaises(ValueError):attach_ovx_context(frame,calendar,context,contract)
            write_contract();data.filter(pl.col('DATE')!=days[1].strftime('%m/%d/%Y')).write_csv(source)
            with self.assertRaises(ValueError):attach_ovx_context(frame,calendar,context,contract)
            write_contract()
            with self.assertRaises(ValueError):attach_ovx_context(frame,calendar,context,contract)
            # 来源未来数值变化并经显式重新登记，不影响历史窗口；真实运行哈希变化仍会被拒绝。
            data.with_columns(pl.when(pl.col('DATE')==days[4].strftime('%m/%d/%Y')).then(900.).otherwise(pl.col('OVX')).alias('OVX')).write_csv(source)
            write_contract();other=attach_ovx_context(frame,calendar,context,contract).collect().sort('date')
            self.assertTrue(actual.equals(other))
            with self.assertRaises(ValueError):attach_ovx_context(frame.with_columns(pl.lit(0.).alias('ovx_change')),calendar,context,contract)
            outside=pl.DataFrame(dict(date=[days[4]],asset=['B'],close=[10.])).lazy()
            with self.assertRaises(ValueError):attach_ovx_context(outside,calendar,context,contract)

            # 仅登记的源日期缺口被传播；上下文不能补零，也不能顺延到其他源日期。
            data.filter(pl.col('DATE')!=days[1].strftime('%m/%d/%Y')).write_csv(source)
            valid_context.with_columns(pl.lit(None,dtype=pl.Float64).alias('ovx_change')).write_parquet(context)
            write_contract(source_missing_dates=[days[1].isoformat()])
            missing=attach_ovx_context(frame,calendar,context,contract).collect()
            self.assertEqual(missing['ovx_change'].null_count(),2)
            write_contract(source_missing_dates=[])
            with self.assertRaises(ValueError):attach_ovx_context(frame,calendar,context,contract)
            valid_context.write_parquet(context);write_contract(source_missing_dates=[days[1].isoformat()])
            with self.assertRaises(ValueError):attach_ovx_context(frame,calendar,context,contract)
