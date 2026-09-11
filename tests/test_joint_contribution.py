"""联合诊断的替代关系、有限预算和因果边界回归。"""
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import polars as pl

from factor_miner.joint_contribution import (
    block_indices, choose_representatives, coalition_design, coalition_key,
    correlation_summary, paired_delta, rolling_predictions, shapley_values, sharpe,
)
from factor_miner.joint_study import (
    JointStudyConfig, common_panel, digest, evaluate_prediction, load_labels,
    register_joint_study, run_joint_study, save_execution,
)
from factor_miner.trading_schedule import RebalanceWindow


class ContributionTests(unittest.TestCase):
    def test_substitutes_are_not_both_removed(self):
        groups = ['A', 'B']
        # 两个同效信号，单独贡献有效；删任一个没有损失，Shapley 各分一半。
        values = {coalition_key(()): .3, coalition_key(['A']): 1.3,
                  coalition_key(['B']): 1.3, coalition_key(groups): 1.3}
        exact = shapley_values(values, groups, [])
        self.assertTrue(all(abs(x['contribution']-.5) < 1e-10 for x in exact))
        sampled = shapley_values(values, groups, [['A', 'B'], ['B', 'A']])
        self.assertAlmostEqual(sum(x['contribution'] for x in sampled), 1.)

    def test_direct_representative_not_correlation_chain(self):
        candidates = [dict(factor_id=f, group='g', coverage=1.) for f in ['A', 'B', 'C']]
        pairs = [dict(left=a, right=b, median_abs_spearman=r) for a, b, r in
                 [('A', 'B', .95), ('B', 'C', .95), ('A', 'C', .3)]]
        rows = choose_representatives(candidates, pairs, .85)
        self.assertEqual([r['factor_id'] for r in rows if r['role'] == '研究代表'], ['A', 'C'])

    def test_coalition_budget_and_determinism(self):
        g = {'A': ['a', 'b'], 'B': ['c']}
        a = coalition_design(g, permutations=16, seed=1, exact=False, max_models=20)
        self.assertEqual(a, coalition_design(g, permutations=16, seed=1, exact=False, max_models=20))
        with self.assertRaises(ValueError):
            coalition_design(g, permutations=16, seed=1, exact=False, max_models=4)
        with self.assertRaises(ValueError):
            coalition_design({'a': ['x'], 'b': ['x']}, permutations=2, seed=1, exact=False, max_models=20)

    def test_correlations_do_not_flatten_time(self):
        rows = []
        for t in range(4):
            for i in range(30):
                rows.append(dict(date=date(2020, 1, 1)+timedelta(days=t), asset=str(i),
                                 a=float(i+t*1000), b=float(-i+t*1000)))
        pair = correlation_summary(pl.DataFrame(rows), ['a', 'b'], 20, 2)[0]
        self.assertAlmostEqual(pair['median_spearman'], -1.)
        self.assertAlmostEqual(pair['median_abs_spearman'], 1.)

    def test_purge_and_future_labels_do_not_change_predictions(self):
        start = date(2020, 1, 1)
        rows = []
        for t in range(20):
            for i in range(30):
                rows.append(dict(date=start+timedelta(days=t*5), asset=str(i),
                    f=float(i-15), target_z=float(i)/20,
                    label_exit_date=start+timedelta(days=t*5+6)))
        frame = pl.DataFrame(rows)
        predict_start = start+timedelta(days=60)
        a, audit = rolling_predictions(frame, {'full': ['f'], 'empty': []}, evaluation_start=predict_start,
            train_signals=12, retrain_signals=100, alpha=1., min_names=20)
        changed = frame.with_columns(pl.when(pl.col('label_exit_date') >= predict_start)
            .then(None).otherwise(pl.col('target_z')).alias('target_z'))
        b, _ = rolling_predictions(changed, {'full': ['f'], 'empty': []}, evaluation_start=predict_start,
            train_signals=12, retrain_signals=100, alpha=1., min_names=20)
        self.assertTrue(a['full'].equals(b['full']))
        self.assertEqual(a['full'].height, 8*30)
        self.assertLess(audit[0]['train_last_label_exit'], predict_start)

    def test_paired_bootstrap_and_undefined_sharpe(self):
        a = np.random.default_rng(1).normal(.001, .01, 200)
        ix = block_indices(200, 20, 100, 9)
        result = paired_delta(a, a, ix)
        self.assertEqual(result['delta_sharpe'], 0.)
        self.assertEqual(result['conditional_ci_high'], 0.)
        with self.assertRaises(ValueError):
            sharpe(np.zeros(100))

    def test_missing_future_fill_keeps_selection_and_cash(self):
        days = [date(2020, 1, 1)+timedelta(days=i) for i in range(4)]
        pred = pl.DataFrame({'date': [days[0]]*10, 'asset': [str(i) for i in range(10)], 'score': [float(i) for i in range(10)]})
        market = pl.DataFrame([dict(trade_date=d, security_id=str(i), open=10.+t+i*.1) for t, d in enumerate(days) for i in range(10)])
        state = market.select('trade_date', 'security_id').with_columns(pl.lit(True).alias('valid_for_factor_rank'),
            ((pl.col('security_id') != '9') | (pl.col('trade_date') != days[1])).alias('can_open_long'), pl.lit(True).alias('can_close_long'))
        schedule = (RebalanceWindow(signal_date=days[0], entry_date=days[1], exit_date=days[3]),)
        r = evaluate_prediction(pred, market, state, schedule, None, 14)
        self.assertEqual([s['security_id'] for s in r.selections], ['9'])
        self.assertEqual(r.execution_summary['final_cash'], 1.)

    def test_unresolved_terminal_keeps_actual_metrics_null(self):
        days = [date(2020, 1, 1)+timedelta(days=i) for i in range(5)]
        pred = pl.DataFrame({'date': [days[0]]*10, 'asset': [str(i) for i in range(10)], 'score': [float(i) for i in range(10)]})
        market = pl.DataFrame([dict(trade_date=d, security_id=str(i), open=10.+t+i*.1) for t, d in enumerate(days) for i in range(10)])
        state = market.select('trade_date', 'security_id').with_columns(pl.lit(True).alias('valid_for_factor_rank'),
            pl.lit(True).alias('can_open_long'), (pl.col('trade_date') < days[3]).alias('can_close_long'))
        schedule = (RebalanceWindow(signal_date=days[0], entry_date=days[1], exit_date=days[4]),)
        result = evaluate_prediction(pred, market, state, schedule, None, 14)
        self.assertEqual(len(result.unresolved_positions), 1)
        with tempfile.TemporaryDirectory() as tmp:
            metrics = save_execution(Path(tmp)/'result', result)
            self.assertIsNone(metrics['actual'])
            self.assertLess(metrics['zero_recovery']['period_return'], metrics['reference']['period_return'])


class StudyFlowTests(unittest.TestCase):
    def test_synthetic_end_to_end_and_immutable_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); dataset = root/'data'; dataset.mkdir()
            rng = np.random.default_rng(123)
            days = [date(2020, 1, 1)+timedelta(days=i) for i in range(260)]
            names = [f'{i:03}' for i in range(30)]
            prices = np.exp(np.cumsum(rng.normal(.0003, .015, (260, 30)), axis=0))*100
            rows, raw = [], []
            for t, d in enumerate(days):
                for i, name in enumerate(names):
                    rows.append(dict(date=d, asset=name, open=float(prices[t, i])))
                    raw.append(dict(date=d, asset=name, raw_factor=float(rng.normal()), valid_for_factor_compute=True))
            market = pl.DataFrame(rows); market.write_parquet(dataset/'market.parquet')
            state = market.select('date', 'asset').with_columns(*[pl.lit(True).alias(c) for c in
                ['valid_for_factor_rank', 'can_open_long', 'can_close_long']])
            state.write_parquet(dataset/'state.parquet')
            pl.DataFrame({'date': days}).write_parquet(dataset/'calendar.parquet')
            labels = [dict(date=d, asset=name, label_entry_date=days[t+1], label_exit_date=days[t+6],
                           label_o2o_5d=float(prices[t+6, i]/prices[t+1, i]-1))
                      for t, d in enumerate(days[:-6]) for i, name in enumerate(names)]
            pl.DataFrame(labels).write_parquet(dataset/'label.parquet')
            pl.DataFrame(schema={'security_id': pl.String, 'event_date': pl.Date, 'available_at': pl.Date,
                                 'terminal_value': pl.Float64, 'source': pl.String}).write_parquet(dataset/'terminal_values.parquet')
            release = dict(market_id='a_share', release_id='synthetic', adjustment='synthetic', calendar_version='synthetic',
                state_version='synthetic', as_of_date=str(days[-1]), state_as_of_date=str(days[-1]))
            (dataset/'release.json').write_text(json.dumps(release))
            candidates = []
            for f in ['huan002', 'huan003']:
                path = root/(f+'.parquet')
                values = pl.DataFrame(raw)
                if f == 'huan003':
                    values = values.with_columns(pl.Series('raw_factor', rng.normal(size=values.height)))
                values.write_parquet(path)
                candidates.append(dict(factor_id=f, raw_path=str(path), original_direction='positive',
                                       group=f, source_candidate_id=f, qualification='research_candidate_pending_confirmation'))
            cfg = dict(collection=str(root/'collection'), expected_candidates=2, discovery_start=str(days[0]),
                discovery_end=str(days[119]), evaluation_start=str(days[120]), evaluation_end=str(days[239]), forbidden_start=str(days[240]),
                train_signals=12, retrain_signals=6, min_dates=20, permutations=4, max_models=20,
                bootstrap_repetitions=20, bootstrap_block_days=10, research_question='合成数据验证完整联合贡献诊断链路')
            config = root/'config.json'; config.write_text(json.dumps(cfg))
            inputs = {str(p.resolve()): digest(p) for p in list(dataset.iterdir()) + [Path(c['raw_path']) for c in candidates]}
            out = root/'run'
            with patch('factor_miner.joint_study.candidate_inputs', return_value=(candidates, inputs, dataset)):
                register_joint_study(config, out)
            run_joint_study(out)
            summary = json.loads((out/'summary.json').read_text())
            self.assertFalse(summary['test_returns_read'])
            self.assertIsNone(summary['formal_pass'])
            self.assertTrue((out/'completion.json').exists())
            self.assertTrue((out/'研究结果.html').exists())
            cfg['replay_from'] = str(out)
            replay_config = root/'replay.json'; replay_config.write_text(json.dumps(cfg))
            replay = root/'replay'
            with patch('factor_miner.joint_study.candidate_inputs', return_value=(candidates, dict(inputs), dataset)):
                register_joint_study(replay_config, replay)
            with patch('factor_miner.joint_study.evaluate_prediction', side_effect=AssertionError('完整缓存不应重算成交')):
                run_joint_study(replay)
            copied = json.loads((replay/'summary.json').read_text())
            self.assertEqual(copied['group_shapley'], summary['group_shapley'])
            self.assertEqual(copied['reduction_comparison'], summary['reduction_comparison'])
            with self.assertRaises(FileExistsError):
                run_joint_study(out)
            (out/'protocol.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, '协议'):
                run_joint_study(out)


if __name__ == '__main__':
    unittest.main()
