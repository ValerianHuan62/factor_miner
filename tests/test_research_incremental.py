"""用尚未发生的标签事件对滚动训练实施污染攻击。"""
from datetime import date
import unittest
import numpy as np
import polars as pl
from factor_miner.research_incremental import fit_purged_ridge, explicit_model_candidates

class IncrementalCausalityTest(unittest.TestCase):
    def test_explicit_selection_cannot_use_confirmation_to_rescue_bad_discovery(self):
        from tests.test_library_review import candidate
        first, second = candidate('huan001'), candidate('huan002')
        second['evaluation']['rank_ic_mean'] = -1
        baseline, added = explicit_model_candidates([first, second], ['huan001'], ['huan002'])
        self.assertEqual(added[0]['factor_id'], 'huan002')
        with self.assertRaises(ValueError):
            explicit_model_candidates([first, second], ['huan001'], ['huan001'])
        second['discovery']['rank_ic_mean'] = -.1
        second['evaluation']['rank_ic_mean'] = 1
        with self.assertRaisesRegex(ValueError, '发现期'):
            explicit_model_candidates([first, second], ['huan001'], ['huan002'])

    def test_future_label_events_cannot_change_coefficients(self):
        history=pl.DataFrame({'date':[date(2023,12,15)]*100,'label_exit_date':[date(2023,12,25)]*100,
            'target_z':[float(i%7) for i in range(100)],'feature':[float(i%11) for i in range(100)]})
        future=history.with_columns(pl.lit(date(2024,1,8)).alias('label_exit_date'),pl.lit(1e9).alias('target_z'))
        base=fit_purged_ridge(history,['feature'],date(2024,1,5),10)
        attacked=fit_purged_ridge(pl.concat([history,future]),['feature'],date(2024,1,5),10)
        np.testing.assert_allclose(base[0],attacked[0])
        self.assertEqual(base[1],attacked[1])
        self.assertEqual(attacked[2]['train_rows'],100)
        self.assertLess(attacked[2]['train_last_label_exit'],date(2024,1,5))
        with self.assertRaises(ValueError):
            fit_purged_ridge(future,['feature'],date(2024,1,5),10)
