"""精简界面的结果边界、市场隔离与新批次交互回归。"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from dashboard.market_profiles import load_market_profiles, default_market_id
from dashboard.results import compact_report, actual_performance, result_row, wealth_figure, _catalog
from dashboard.worker_connection import submit_start_cli
from dashboard.research_control import load_report_hypotheses
from dashboard.real_backtest import load_real_backtest_catalog, ACCOUNTING_BACKTEST_SCHEMA
from factor_miner.autonomous_schema import ResearchCommand, AutonomousResearchState, AutonomousStage
from factor_miner.research_control import ResearchControlStore


NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)


def profile_file(root):
    """测试只写临时目录，不触碰真实控制队列。"""
    path = root / 'markets.json'
    path.write_text(json.dumps({'markets': [
        {'market_id': market, 'display_name': name, 'data_root': str(root),
         'artifact_root': str(root / market), 'market_path': 'market.parquet',
         'state_path': 'state.parquet', 'market_columns': {'open': 'open'}}
        for market, name in [('a_share', 'A 股'), ('us_equity', '美股')]
    ]}))
    return path


class SimpleDashboardTest(unittest.TestCase):
    """执行真实 Streamlit 页面事件，避免仅检查源码字符串。"""

    def test_cross_market_novelty_appendix_keeps_stats_and_rejects_new_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'dashboard_backtests').mkdir()
            (root/'dashboard_backtests/huan999.json').write_text(json.dumps({'schema_version':ACCOUNTING_BACKTEST_SCHEMA}))
            (root/'screening_summary.json').write_text(json.dumps({'by_factor':{'huan999':{'statistical_pass':True,'incremental_pass':False,'label':'统计通过','reasons':[]}},'final_count':0}))
            (root/'legacy_structure_audit.json').write_text(json.dumps({'comparisons':[{'factor_id':'huan999','parameter_or_sign_family_matches':['huan001']}],'semantic':[]}))
            payload={key:{} for key in ('evaluation','protocol','data_identity','window')}
            payload.update(factor_id='huan999',factor_name='合成候选',market_id='us_equity',mode='合成',scope='合成')
            with patch('dashboard.real_backtest.load_real_backtest',return_value=payload):
                result=load_real_backtest_catalog(backtest_roots=(root/'dashboard_backtests',))[0]
            self.assertTrue(result['screening']['statistical_pass'])
            self.assertFalse(result['screening']['novelty_pass'])
            self.assertEqual(result['screening']['label'],'统计通过 · 已知机制')
            self.assertTrue(any('A 股 huan001' in reason for reason in result['screening']['reasons']))
            self.assertIn('统计备选 1 个',result['batch_summary'])
            self.assertIn('跨市场已知机制 1 个',result['batch_summary'])

    def test_default_prefers_us_even_when_a_share_is_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(default_market_id(load_market_profiles(profile_file(root))), 'us_equity')

    def test_unknown_actual_values_never_borrow_reference_returns(self):
        report = compact_report({'evaluation': {'ic_mean': .1, 'rank_ic_mean': .2},
            'factor_id': 'huan001', 'factor_name': '测试候选', 'reference_metrics': {'annualized_return': .3},
            'unresolved_positions': [{'security_id': 'S'}]})
        self.assertEqual(actual_performance(report), {})
        self.assertEqual(result_row(report)['年化收益'], .3)
        self.assertEqual(result_row(report)['筛选状态'], '参考估值')
        self.assertEqual(result_row(report)['IC'], .1)

    def test_unknown_benchmark_keeps_information_ratio_missing(self):
        report = {'pending_count': 0, 'benchmark_pending_count': 1,
                  'reference_metrics': {'annualized_return': .1, 'information_ratio': 2}}
        self.assertEqual(actual_performance(report)['annualized_return'], .1)
        self.assertIsNone(actual_performance(report)['information_ratio'])

    def test_historical_hypotheses_are_grouped_without_fabricated_tags(self):
        hypothesis = {"claim": "测试趋势假设", "mechanism": "测试机制", "expected_sign": "positive",
            "observable_proxy": "收益变化", "independent_verification": "分层检验",
            "competing_explanations": ["行业变化"], "failure_modes": ["趋势反转"],
            "falsification_path": "独立区间不成立", "source_refs": ["合成来源"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run_manifest.json").write_text(json.dumps({"run_id": "test_run", "reports": [
                {"hypothesis_id": "H01", "hypothesis": hypothesis},
                {"hypothesis_id": "H01", "hypothesis": hypothesis}]}))
            rows = load_report_hypotheses((root / "dashboard_backtests",))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["claim_zh"], "测试趋势假设")
            self.assertIsNone(rows[0]["semantic_plan"])
            self.assertNotIn("decision", rows[0])

    def test_cross_market_report_is_rejected(self):
        _catalog.clear()
        with patch('dashboard.results.load_real_backtest_catalog', return_value=[{'market_id': 'a_share'}]):
            with self.assertRaisesRegex(ValueError, '市场'):
                _catalog('us_equity', None, ('/tmp/test-cross-market',))

    def test_wealth_drawdown_uses_initial_capital(self):
        rows = [{'exit_date': '2026-01-01', 'target_long_net_return': -.1, 'benchmark_return': 0},
                {'exit_date': '2026-01-02', 'target_long_net_return': .1, 'benchmark_return': 0}]
        figure = wealth_figure(rows, drawdown=True)
        self.assertAlmostEqual(figure.data[0].y[0], -.1)
        self.assertAlmostEqual(figure.data[0].y[1], -.01)
        rows[0]['target_long_net_return'] = None
        with self.assertRaises(ValueError):
            wealth_figure(rows)

    def test_start_cli_is_idempotent_and_distinct_pending_start_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = submit_start_cli(root, NOW)
            self.assertEqual(first, submit_start_cli(root, NOW))
            with self.assertRaises(RuntimeError):
                submit_start_cli(root, NOW + timedelta(seconds=1))
            self.assertEqual(len(ResearchControlStore(root).pending_commands()), 1)

    def test_workbench_registers_batch_and_keeps_natural_language_tab_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict('os.environ', {'FM_MARKET_PROFILES_PATH': str(profile_file(root))}):
                page = AppTest.from_file('dashboard/pages/7_研究运行台.py', default_timeout=30).run()
                self.assertFalse(page.exception)
                next(control for control in page.radio if control.label == '研究流程').set_value('历史批次流程').run()
                self.assertEqual([tab.label for tab in page.tabs], ['研究批次', '自然语言分区'])
                next(button for button in page.button if button.label == '新建研究批次').click().run()
                page.run()
                self.assertFalse(page.exception)
                store = ResearchControlStore(root / 'us_equity')
                self.assertEqual(len(store.pending_commands()), 1)
                self.assertTrue(any('已登记' in message.value for message in page.info))
                self.assertFalse(any(button.label == '新建研究批次' for button in page.button))
                self.assertEqual(len(page.tabs), 2)

    def test_next_batch_after_complete_and_all_rejected_gets_new_command(self):
        for terminal in (AutonomousStage.COMPLETED, AutonomousStage.NO_APPROVED_HYPOTHESIS):
            with self.subTest(stage=terminal), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.dict('os.environ', {'FM_MARKET_PROFILES_PATH': str(profile_file(root))}):
                    store = ResearchControlStore(root / 'us_equity')
                    old = ResearchCommand.start(requested_by='research_owner', requested_at=NOW)
                    store.submit(old)
                    store.mark_processed(old.command_id, processed_at=NOW)
                    store.publish_state(AutonomousResearchState.build(
                        run_id='autrun_' + '1' * 24, sequence=0, stage=terminal,
                        created_at=NOW, updated_at=NOW, command_ids=(old.command_id,), stage_refs={}))
                    page = AppTest.from_file('dashboard/pages/7_研究运行台.py', default_timeout=30).run()
                    next(control for control in page.radio if control.label == '研究流程').set_value('历史批次流程').run()
                    next(button for button in page.button if button.label == '新建下一批研究').click().run()
                    self.assertFalse(page.exception)
                    pending = store.pending_commands()
                    self.assertEqual(len(pending), 1)
                    self.assertNotEqual(pending[0].command_id, old.command_id)
                    self.assertTrue(any('已登记' in message.value for message in page.info))

    def test_home_without_database_opens_us_results_and_empty_search_is_safe(self):
        report = compact_report({'factor_id': 'huan001', 'factor_name': '测试因子', 'market_id': 'us_equity',
            'evaluation': {'ic_mean': .1, 'rank_ic_mean': .2, 'rank_ic_hac_t': 2},
            'window': {'start': '2026-01-01', 'end': '2026-01-02'}, 'scope': '合成回归',
            'daily_rows': [{'entry_date': '2025-12-31', 'exit_date': '2026-01-01', 'target_long_net_return': .1, 'target_long_gross_return': .101, 'target_long_turnover': 2, 'cost_turnover_sides': 2, 'benchmark_return': .01},
                           {'entry_date': '2026-01-01', 'exit_date': '2026-01-02', 'target_long_net_return': -.05, 'target_long_gross_return': -.049, 'target_long_turnover': 2, 'cost_turnover_sides': 2, 'benchmark_return': .01}]})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict('os.environ', {'FM_MARKET_PROFILES_PATH': str(profile_file(root))}), patch(
                'dashboard.results.reports_for_market', return_value=[report]):
                page = AppTest.from_file('dashboard/app.py', default_timeout=30).run()
                self.assertFalse(page.exception)
                self.assertEqual(page.title[0].value, '看结果')
                market = next(select for select in page.selectbox if select.label == '研究市场')
                self.assertEqual(market.value, 'us_equity')
                self.assertEqual([item.label for item in page.metric], ['IC', 'RankIC', 't 值', '年化收益', 'Sharpe', '最大回撤'])
                self.assertEqual(page.metric[0].value, '0.1000')
                self.assertEqual(page.metric[1].value, '0.2000')
                columns = json.loads(page.dataframe[0].proto.columns)
                self.assertEqual(columns['IC']['type_config']['format'], '%.4f')
                self.assertEqual(columns['RankIC']['type_config']['format'], '%.4f')
                before = next(item.value for item in page.metric if item.label == '年化收益')
                next(item for item in page.number_input if item.label == '额外往返滑点（bps）').set_value(20.0).run()
                self.assertFalse(page.exception)
                self.assertNotEqual(before, next(item.value for item in page.metric if item.label == '年化收益'))
                page.text_input[0].set_value('没有这样的因子').run()
                self.assertFalse(page.exception)
                self.assertTrue(any('没有匹配' in item.value for item in page.info))


if __name__ == '__main__':
    unittest.main()
