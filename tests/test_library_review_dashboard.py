"""复核读模型的身份、原结果隔离与页面筛选回归。"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from dashboard.library_review import apply_library_review, apply_research_pool
from dashboard.results import compact_report, matches_result_kind, result_row
from tests.test_dashboard_simple import profile_file


class ReviewDashboardTest(unittest.TestCase):
    def test_research_pool_keeps_original_failure_and_checks_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'huan001.json'; source.write_text('{}')
            report=dict(factor_id='huan001', source_candidate_id='candidate', report_batch=str(root),
                factor_name='弱互补信号', evaluation={}, library_review=dict(status='rejected', label='未保留', statistical_pass=False))
            pool=dict(schema_version='research-pool-dashboard-v1', market_id='us_equity', message='探索性复核',
                by_factor={'huan001':dict(source_candidate_id='candidate',report_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    status='research_input',previous_status='rejected')})
            path=root/'pool.json';path.write_text(json.dumps(pool))
            result=apply_research_pool([report],path,'us_equity')[0]
            self.assertEqual(result['library_review']['statistical_pass'],False)
            self.assertEqual(result_row(result)['筛选状态'],'未保留')
            self.assertEqual(result_row(result)['组合研究'],'研究代表')
            self.assertTrue(matches_result_kind(result,'组合研究池'))
            self.assertTrue(matches_result_kind(result,'恢复研究'))
            self.assertTrue(matches_result_kind(result,'淘汰档案'))
            self.assertNotIn('research_pool',report)
            with self.assertRaises(ValueError):apply_research_pool([report],path,'a_share')
            source.write_text('{"changed":true}')
            with self.assertRaises(ValueError):apply_research_pool([report],path,'us_equity')

    def test_review_requires_matching_market_and_original_report_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'huan001.json'
            source.write_text('{"test": 1}')
            report = {'factor_id': 'huan001', 'source_candidate_id': 'candidate', 'report_batch': str(root),
                      'screening': {'label': '原始统计结论'}}
            review = {'schema_version': 'library-review-v1', 'market_id': 'us_equity', 'message': '测试',
                      'by_factor': {'huan001': {'source_candidate_id': 'candidate',
                          'report_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'label': '保留基底'}}}
            path = root / 'review.json'
            path.write_text(json.dumps(review))
            result = apply_library_review([report], path, 'us_equity')[0]
            self.assertEqual(result['library_review']['label'], '保留基底')
            self.assertNotIn('library_review', report)
            self.assertEqual(result['screening'], report['screening'])
            with self.assertRaisesRegex(ValueError, '市场'):
                apply_library_review([report], path, 'a_share')
            source.write_text('{"test": 2}')
            with self.assertRaisesRegex(ValueError, '身份'):
                apply_library_review([report], path, 'us_equity')

    def test_home_excludes_rejected_but_archive_keeps_diagnostics(self):
        reports = []
        for fid, status, label in [('huan001', 'retained', '保留基底'), ('huan002', 'watch', '观察池'),
                                   ('huan003', 'reserve', '同类替补'), ('huan004', 'rejected', '未保留')]:
            report = compact_report({'factor_id': fid, 'factor_name': '合成候选', 'market_id': 'us_equity',
                'evaluation': {'ic_mean': -.012345, 'rank_ic_mean': -.023456, 'rank_ic_hac_t': -4},
                'window': {'start': '2026-01-01', 'end': '2026-01-02'}, 'scope': '测试',
                'daily_rows': [{'entry_date': '2025-12-31', 'exit_date': '2026-01-01', 'target_long_net_return': .01,
                                'target_long_gross_return': .011, 'target_long_turnover': 2, 'cost_turnover_sides': 2, 'benchmark_return': 0},
                               {'entry_date': '2026-01-01', 'exit_date': '2026-01-02', 'target_long_net_return': -.01,
                                'target_long_gross_return': -.009, 'target_long_turnover': 2, 'cost_turnover_sides': 2, 'benchmark_return': .001}]})
            report.update(library_review={'status': status, 'label': label, 'reasons': []},
                          library_summary='合成复核', library_default_view='因子')
            reports.append(report)
        with tempfile.TemporaryDirectory() as directory:
            path = profile_file(Path(directory))
            with patch.dict('os.environ', {'FM_MARKET_PROFILES_PATH': str(path)}), patch('dashboard.results.reports_for_market', return_value=reports):
                page = AppTest.from_file('dashboard/app.py', default_timeout=30).run()
                self.assertFalse(page.exception)
                self.assertEqual(next(x for x in page.selectbox if x.label == '查看').value, '在研因子')
                self.assertEqual(len(next(x for x in page.selectbox if x.label == '当前因子').options), 3)
                self.assertEqual(next(x for x in page.selectbox if x.label == '当前因子').value, 'huan001')
                self.assertEqual(page.metric[0].value, '-0.0123')
                self.assertEqual(page.metric[1].value, '-0.0235')
                next(x for x in page.selectbox if x.label == '查看').set_value('观察池').run()
                self.assertFalse(page.exception)
                self.assertEqual(next(x for x in page.selectbox if x.label == '当前因子').value, 'huan002')
                next(x for x in page.selectbox if x.label == '查看').set_value('淘汰档案').run()
                self.assertFalse(page.exception)
                self.assertEqual(next(x for x in page.selectbox if x.label == '当前因子').value, 'huan004')
                self.assertEqual(page.metric[1].value, '-0.0235')
                next(x for x in page.selectbox if x.label == '查看').set_value('全部').run()
                self.assertFalse(page.exception)
                self.assertEqual(len(next(x for x in page.selectbox if x.label == '当前因子').options), 4)
