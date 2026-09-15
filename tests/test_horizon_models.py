"""多期限模型保持未知收益的预测与长标签的因果训练边界。"""
from datetime import date, timedelta
import json

import numpy as np
import polars as pl

from factor_miner.horizon_models import model_rank_ic, run_horizon_models
from factor_miner.ridge_strategy import file_hash, join_targets, walk_forward


def test_long_target_purge_and_unknown_prediction_are_preserved():
    days = [date(2023, 1, 6) + timedelta(weeks=i) for i in range(15)]
    rows = [dict(date=d, asset=str(i), f=float(i), label_o2o_20d=None if i == 0 else float(i % 7),
                 label_exit_date=d + timedelta(days=28)) for d in days for i in range(40)]
    raw = pl.DataFrame(rows)
    panel = join_targets(raw.select('date', 'asset', 'f').lazy(),
                         raw.select('date', 'asset', 'label_o2o_20d', 'label_exit_date').lazy(), label_column='label_o2o_20d')
    policy = dict(train_weeks=8, embargo_weeks=1, retrain_weeks=2, alpha=10.)
    original, fits = walk_forward(panel, days, {'m': ['f']}, policy, days[9], label_column='label_o2o_20d')
    attacked = panel.with_columns(pl.when(pl.col('label_exit_date') >= days[9]).then(1e9).otherwise(pl.col('target_z')).alias('target_z'))
    changed, _ = walk_forward(attacked, days, {'m': ['f']}, policy, days[9], label_column='label_o2o_20d')
    np.testing.assert_allclose(original.filter(pl.col('date') < days[11])['prediction'], changed.filter(pl.col('date') < days[11])['prediction'])
    assert all(f['train_last_label_exit'] < f['prediction_start'] for f in fits)
    assert original['label_o2o_20d'].null_count() == 6
    assert 'label_o2o_5d' not in original.columns


def test_evaluation_purges_cross_year_without_deleting_predictions():
    rows = [dict(date=day, asset=str(i), model='m', prediction=float(i), label_o2o_20d=float(i % 9))
            for day in [date(2024, 11, 1), date(2024, 12, 20)] for i in range(30)]
    predictions = pl.DataFrame(rows)
    labels = predictions.select('date', 'asset').with_columns((pl.col('date') + pl.duration(days=28)).alias('label_exit_date'))
    result = model_rank_ic(predictions, labels, 'label_o2o_20d')
    assert result['date'].to_list() == [date(2024, 11, 1)]
    assert predictions.height == 60


def test_three_horizon_runner_keeps_dynamic_targets_and_budget(tmp_path):
    rng = np.random.default_rng(729)
    days = [date(2022, 1, 3) + timedelta(days=i) for i in range(1450)]
    days = [d for d in days if d.weekday() < 5]
    last = {d.isocalendar()[:2]: d for d in days}
    signals_days = list(last.values())[:-6]
    calendar = tmp_path / 'calendar.parquet'
    pl.DataFrame({'date': days}).write_parquet(calendar)
    panel = pl.DataFrame([dict(date=d, asset=str(i), f=float(rng.normal()), g=float(rng.normal()))
                          for d in signals_days for i in range(30)])
    source_panel = tmp_path / 'source.parquet'
    panel.write_parquet(source_panel)
    labels = {}
    positions = {d: i for i, d in enumerate(days)}
    for h in [1, 5, 20]:
        path = tmp_path / f'label_{h}.parquet'
        label = panel.select('date', 'asset').with_columns(
            pl.Series(f'label_o2o_{h}d', panel['f'].to_numpy() * .01 + rng.normal(0, .02, panel.height)),
            pl.Series('label_exit_date', [days[positions[d] + h + 1] for d in panel['date']]))
        label.write_parquet(path)
        labels[str(h)] = str(path)
    policy = dict(train_weeks=40, embargo_weeks=1, retrain_weeks=12, alpha=10.)
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(dict(model_policy=policy, history_start=str(signals_days[0]), evaluation_end=str(signals_days[-1]))))
    config = dict(output_root=str(tmp_path / 'run'), horizons=[1, 5, 20], models={'base': ['f'], 'joint': ['f', 'g']},
        comparisons=[dict(left='joint', right='base')], features=['f', 'g'], test_consumed=True, new_formula_capacity=0,
        signal_sampling='weekly_last_session', model_capacity=6, comparison_capacity=3, diagnostic_family_size=109,
        inherited_diagnostic_family_size=100, source_panel_path=str(source_panel), source_protocol_path=str(source),
        calendar_path=str(calendar), label_paths=labels, model_policy=policy, evaluation_start='2023-01-01',
        hac_week_lags={'1': 5, '5': 5, '20': 5}, expected_signs={'f': 'positive', 'g': 'negative'})
    config['input_sha256'] = {str(p): file_hash(p) for p in [source, source_panel, calendar, *map(type(tmp_path), labels.values())]}
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps(config))
    output = run_horizon_models(config_path)
    summary = json.loads((output / 'summary.json').read_text())
    assert len(summary['rows']) == 3
    assert summary['qualified_additions'] == 0 and not summary['execution_or_cost_tested']
    for h in [1, 5, 20]:
        predictions = pl.read_parquet(output / f'{h}d/predictions.parquet')
        assert f'label_o2o_{h}d' in predictions.columns
        fits = json.loads((output / f'{h}d/fit_audit.json').read_text())
        assert all(x['train_last_label_exit'] < x['prediction_start'] for x in fits)
