"""组合研究资格不改旧统计结论，不读取确认结果，不让失败代表阻断新增。"""
import copy
import unittest

from factor_miner.research_pool import research_admission, select_pool, validate_search_budget
from factor_miner.research_incremental import explicit_model_candidates
from tests.test_library_review import candidate


def report(fid, t=4):
    value = candidate(fid, t=t)
    value['ast_hash'] = fid
    return value


class ResearchPoolTests(unittest.TestCase):
    def test_weak_signal_can_be_researched_without_changing_old_p(self):
        value = report('huan001')
        value['discovery'].update(rank_ic_mean=.003, bonferroni_p_value=1.)
        before = copy.deepcopy(value)
        self.assertEqual(research_admission(value)['status'], 'eligible')
        value['evaluation']['rank_ic_mean'] = -1e6
        self.assertEqual(research_admission(value)['status'], 'eligible')
        self.assertEqual(value['discovery'], before['discovery'])
        self.assertEqual(research_admission(value, exclusions=('测量失败',))['status'], 'excluded')
        value['discovery']['rank_ic_mean'] = -.003
        self.assertEqual(research_admission(value)['status'], 'excluded')

    def test_failed_historical_candidate_is_not_an_active_representative(self):
        a, failed, new = report('huan001'), report('huan002', 100), report('huan003')
        failed['discovery']['rank_ic_mean'] = -.1
        reports = [a, failed, new]
        admissions = {r['factor_id']: research_admission(r) for r in reports}
        calls = []
        def measure(left, right):
            calls.append((left, right))
            return dict(valid_dates=600, p95_abs_spearman=.2)
        selected, _ = select_pool(reports, admissions, measure, anchors=['huan001'])
        self.assertEqual(selected, ['huan001', 'huan003'])
        self.assertEqual(calls, [('huan003', 'huan001')])

    def test_current_duplicate_and_unknown_overlap_do_not_enter_pool(self):
        reports = [report('huan001'), report('huan002')]
        def execute(value):
            adm = {r['factor_id']: research_admission(r) for r in reports}
            result = select_pool(reports, adm, lambda *_: dict(valid_dates=600, p95_abs_spearman=value), anchors=['huan001'])
            return result, adm
        result, adm = execute(.9)
        self.assertEqual(result[0], ['huan001'])
        self.assertEqual(adm['huan002']['representative'], 'huan001')
        with self.assertRaises(ValueError):
            execute(None)

    def test_legacy_incremental_gate_is_preserved_and_new_route_is_explicit(self):
        values = [report('huan001'), report('huan002')]
        values[1]['discovery'].update(rank_ic_mean=.001, bonferroni_p_value=1.)
        with self.assertRaises(ValueError):
            explicit_model_candidates(values, ['huan001'], ['huan002'])
        pool = dict(schema_version='research-pool-v1', selected_factor_ids=['huan001', 'huan002'],
            by_factor={r['factor_id']: {**research_admission(r), 'status':'research_input'} for r in values})
        self.assertEqual(len(explicit_model_candidates(values, ['huan001'], ['huan002'], pool)[1]), 1)
        pool['by_factor']['huan002']['source_candidate_id'] = 'different'
        with self.assertRaises(ValueError):
            explicit_model_candidates(values, ['huan001'], ['huan002'], pool)

    def test_budget_counts_failed_variants_and_refuses_missing_data(self):
        plan = dict(candidate_capacity=4, model_capacity=2, historical_attempt_count=97,
            inherited_diagnostic_family_size=11901, mechanisms={'trend':dict(capacity=2,
                information_source='prices', data_ready=True, horizon_rationale='冻结持有期依据')})
        proposals = [dict(trial_id=str(i), mechanism_id='trend', information_source='prices', status='failed') for i in range(2)]
        self.assertEqual(validate_search_budget(plan, proposals)['diagnostic_family_size'], 11907)
        with self.assertRaises(ValueError):
            validate_search_budget(plan, proposals+[dict(proposals[0], trial_id='3')])
        plan['mechanisms']['trend']['data_ready'] = False
        with self.assertRaises(ValueError):
            validate_search_budget(plan, proposals)

    def test_shared_statistics_match_separate_models_and_purge_future(self):
        from datetime import date
        import numpy as np
        import polars as pl
        from factor_miner.research_incremental import fit_purged_ridge, fit_purged_ridge_models
        rng = np.random.default_rng(42)
        frame = pl.DataFrame(dict(date=[date(2023,12,1)]*200,
            label_exit_date=[date(2023,12,8)]*150+[date(2024,2,1)]*50,
            target_z=rng.normal(size=200), a=rng.normal(size=200), b=rng.normal(size=200)))
        day=date(2024,1,1); models={'one':['a'], 'joint':['a','b'], 'reverse':['b','a']}
        group=fit_purged_ridge_models(frame, models, day, 10.)
        for name, columns in models.items():
            separate=fit_purged_ridge(frame, columns, day, 10.)
            np.testing.assert_allclose(group[name][0], separate[0], atol=1e-14)
            self.assertAlmostEqual(group[name][1], separate[1])
            self.assertEqual(group[name][2]['train_rows'], 150)
