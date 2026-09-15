"""冻结预算、节假日调仓与观察条件的合成回归。"""
from datetime import date, timedelta
import unittest
import tempfile
import json
from pathlib import Path
import polars as pl
from factor_miner.research_report import report_schedule, ic_diagnostics, run_report, report_direction
from factor_miner.construct_validation import ObservableCondition, validate_construct

class ReportContractTest(unittest.TestCase):
    def test_review_preserves_original_direction_even_when_observed_sign_reverses(self):
        self.assertEqual(report_direction('original_hypothesis','positive',-.1),'positive')
        self.assertEqual(report_direction('original_hypothesis','negative',.1),'negative')
        self.assertEqual(report_direction('discovery','positive',-.1),'negative')
        with self.assertRaises(ValueError):
            report_direction('unknown','positive',.1)

    def test_intraday_response_distinguishes_close_only_momentum(self):
        close={'op':'field','field':'close'}
        opening={'op':'field','field':'open'}
        condition=ObservableCondition(observation='日内强度',measurement='开收盘相对位移',response_test='intraday_strength')
        self.assertEqual(validate_construct({'op':'div','args':[close,opening]},condition)['status'],'基础检验符合')
        momentum={'op':'div','args':[close,{'op':'delay','args':[close],'period':20}]}
        self.assertEqual(validate_construct(momentum,condition)['status'],'偏离')

    def test_duplicate_start_cannot_append_interruption_to_existing_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            output=root/'run'
            output.mkdir()
            (output/'protocol.json').write_text('{}')
            config=root/'config.json'
            config.write_text(json.dumps({'output_root':str(output)}))
            with self.assertRaisesRegex(ValueError,'重复启动'):
                run_report(config)
            self.assertFalse((output/'ledger').exists())

    def test_week_end_uses_calendar_and_does_not_advance_partial_week(self):
        days = [date(2026, 4, 1) + timedelta(days=i) for i in range(35)]
        days = [d for d in days if d.weekday() < 5 and d != date(2026, 4, 3)]
        schedule = report_schedule(days, date(2026,4,1), date(2026,4,14), 'weekly_last_session')
        self.assertEqual([w.signal_date for w in schedule], [date(2026,4,2),date(2026,4,10)])
        self.assertEqual(schedule[0].entry_date, date(2026,4,6))
        self.assertEqual(schedule[0].exit_date, date(2026,4,13))
        with self.assertRaises(ValueError):
            report_schedule(days, days[0], days[-1], 'weekly_last_session')

    def test_budget_adjustment_preserves_raw_significance(self):
        rows = []
        for i in range(90):
            for asset in range(30):
                rows.append({'date':date(2021,1,1)+timedelta(days=i),'asset':str(asset),
                    'raw_factor':float(asset), 'label_o2o_5d':float((asset*(i%11+1)+i)%31),
                    'valid_for_factor_compute':True,'valid_for_factor_rank':True,
                    'label_exit_date':date(2021,1,7)+timedelta(days=i)})
        data=pl.DataFrame(rows).lazy()
        factor=data.select('date','asset','raw_factor','valid_for_factor_compute')
        state=data.select('date','asset','valid_for_factor_rank')
        labels=data.select('date','asset','label_o2o_5d','label_exit_date')
        args=(factor,state,labels,date(2021,1,1),date(2021,4,1),None)
        _, twelve=ic_diagnostics(*args,family_size=12)
        _, thirty=ic_diagnostics(*args,family_size=30)
        self.assertEqual(twelve['raw_p_value'],thirty['raw_p_value'])
        self.assertEqual(twelve['bonferroni_p_value'],min(1,12*twelve['raw_p_value']))
        self.assertEqual(thirty['bonferroni_p_value'],min(1,30*thirty['raw_p_value']))

    def test_volume_condition_rejects_unrelated_price_measurement(self):
        volume={'op':'field','field':'volume'}
        ratio={'op':'div','args':[volume,{'op':'rolling_mean','args':[volume],'window':60}]}
        condition=ObservableCondition(observation='相对放量',measurement='量比',response_test='volume_growth')
        self.assertEqual(validate_construct(ratio,condition)['status'],'基础检验符合')
        wrong={'op':'div','args':[{'op':'field','field':'close'},{'op':'field','field':'open'}]}
        self.assertEqual(validate_construct(wrong,condition)['status'],'偏离')
