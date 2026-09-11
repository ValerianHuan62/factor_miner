"""既有信号的分组、删除实验与重训子集 Shapley；不改变单因子资格。"""
from __future__ import annotations

import math
from itertools import combinations

import numpy as np
import polars as pl


def correlation_summary(panel: pl.DataFrame, features: list[str], min_names: int,
                        min_dates: int) -> list[dict]:
    """在相同完整截面逐日计算 Pearson 与 Spearman，保留绝对相关分布。"""
    pearson, spearman = [], []
    for day in panel.sort('date', 'asset').partition_by('date', maintain_order=True):
        x = day.select(features).to_numpy()
        if len(x) < min_names or not np.isfinite(x).all():
            raise ValueError('共同截面缺数或证券不足，不能改变子集股票池')
        ranks = day.select(pl.col(f).rank(method='average') for f in features).to_numpy()
        with np.errstate(invalid='ignore', divide='ignore'):
            pearson.append(np.corrcoef(x, rowvar=False))
            spearman.append(np.corrcoef(ranks, rowvar=False))
    rows = []
    for i, j in combinations(range(len(features)), 2):
        p = np.array([v[i, j] for v in pearson])
        s = np.array([v[i, j] for v in spearman])
        valid = np.isfinite(p) & np.isfinite(s)
        if valid.sum() < min_dates:
            raise ValueError(f'{features[i]}/{features[j]} 有效相关日期不足')
        rows.append(dict(left=features[i], right=features[j], valid_dates=int(valid.sum()),
                         median_pearson=float(np.median(p[valid])),
                         median_spearman=float(np.median(s[valid])),
                         median_abs_spearman=float(np.median(abs(s[valid]))),
                         p95_abs_spearman=float(np.quantile(abs(s[valid]), .95))))
    return rows


def choose_representatives(candidates: list[dict], pairs: list[dict], threshold: float) -> list[dict]:
    """同机制内按发现期覆盖、稳定编号选代表；每个替补直接满足代表相关阈值。"""
    corr = {frozenset((r['left'], r['right'])): r['median_abs_spearman'] for r in pairs}
    kept, rows = [], []
    for c in sorted(candidates, key=lambda c: (-c['coverage'], c['factor_id'])):
        matches = [k for k in kept if k['group'] == c['group'] and
                   corr[frozenset((c['factor_id'], k['factor_id']))] >= threshold]
        representative = matches[0] if matches else c
        if not matches:
            kept.append(c)
        rows.append(dict(factor_id=c['factor_id'], group=c['group'],
                         representative=representative['factor_id'],
                         role='同类替补' if matches else '研究代表',
                         selection_uses_returns=False))
    return sorted(rows, key=lambda r: r['factor_id'])


def coalition_design(groups: dict[str, list[str]], *, permutations: int, seed: int,
                     exact: bool, max_models: int) -> tuple[dict[str, list[str]], list[list[str]]]:
    """先生成完整有限模型清单；相同子集只拟合一次。"""
    keys = sorted(groups)
    flat = [f for g in keys for f in groups[g]]
    if not keys or any(not groups[g] for g in keys) or len(flat) != len(set(flat)):
        raise ValueError('分组必须非空、互斥且完整')
    paths = []
    if exact:
        if 2 ** len(keys) + len(flat) + 2 > max_models:
            raise ValueError('精确子集超出冻结模型预算')
        subsets = [s for n in range(len(keys) + 1) for s in combinations(keys, n)]
    else:
        if permutations < 2:
            raise ValueError('随机排列至少两条，才能估计排列误差')
        rng = np.random.default_rng(seed)
        paths = [list(rng.permutation(keys)) for _ in range(permutations)]
        subsets = [tuple(path[:i]) for path in paths for i in range(len(keys) + 1)]
    subsets += [tuple(keys), ()] + [tuple(g for g in keys if g != key) for key in keys]
    models = {coalition_key(s): sorted(f for g in s for f in groups[g]) for s in subsets}
    models.update({'drop:' + f: sorted(x for x in flat if x != f) for f in flat})
    if len(models) + 2 > max_models:
        raise ValueError('子集、删除与精简对照超出冻结模型预算')
    return models, paths


def coalition_key(groups) -> str:
    """因子组组合的稳定键；组名禁止分隔符。"""
    if any('|' in g for g in groups):
        raise ValueError('组名不能含分隔符')
    return 'groups:' + '|'.join(sorted(groups))


def sharpe(values: np.ndarray, periods: float = 252.) -> float:
    """使用日收益计算指定情景的 Sharpe；零方差没有定义。"""
    x = np.asarray(values, dtype=float)
    if len(x) < 2 or not np.isfinite(x).all() or np.std(x, ddof=1) <= 1e-14:
        raise ValueError('Sharpe 需要至少两个有限且非零方差收益')
    return float(x.mean() / x.std(ddof=1) * math.sqrt(periods))


def shapley_values(values: dict[str, float], groups: list[str], paths: list[list[str]]) -> list[dict]:
    """价值函数来自重训策略，分解相对明确无信号基准的表现差。"""
    n = len(groups)
    rows = []
    for group in groups:
        if paths:
            samples = []
            for path in paths:
                i = path.index(group)
                samples.append(values[coalition_key(path[:i+1])] - values[coalition_key(path[:i])])
            estimate = float(np.mean(samples))
            se = float(np.std(samples, ddof=1) / math.sqrt(len(samples)))
        else:
            rest = [g for g in groups if g != group]
            estimate = 0.
            for size in range(n):
                weight = 1. / (n * math.comb(n-1, size))
                for subset in combinations(rest, size):
                    estimate += weight * (values[coalition_key((*subset, group))] - values[coalition_key(subset)])
            se = 0.
        rows.append(dict(group=group, contribution=estimate, permutation_se=se))
    total = values[coalition_key(groups)] - values[coalition_key(())]
    if not np.isclose(sum(r['contribution'] for r in rows), total, atol=1e-10):
        raise ValueError('Shapley 加总与基准差不一致')
    return rows


def block_indices(length: int, block: int, repetitions: int, seed: int) -> np.ndarray:
    """所有模型共用日期分块抽样，保留配对比较与块内时间依赖。"""
    if block < 1 or length < block * 2 or repetitions < 2:
        raise ValueError('分块抽样参数或收益日期不足')
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, length, size=(repetitions, math.ceil(length / block)))
    return ((starts[..., None] + np.arange(block)) % length).reshape(repetitions, -1)[:, :length]


def paired_delta(left: np.ndarray, right: np.ndarray, indices: np.ndarray) -> dict:
    """固定预测下的配对分块区间；不覆盖候选筛选与重新训练的不确定性。"""
    if len(left) != len(right):
        raise ValueError('比较日期必须一致')
    samples = np.array([sharpe(left[i]) - sharpe(right[i]) for i in indices])
    return dict(delta_sharpe=sharpe(left) - sharpe(right),
                conditional_ci_low=float(np.quantile(samples, .025)),
                conditional_ci_high=float(np.quantile(samples, .975)),
                bootstrap_positive_ratio=float(np.mean(samples > 0)))


def fit_rolling_models(panel: pl.DataFrame, models: dict[str, list[str]], *,
                        evaluation_start, train_signals: int, retrain_signals: int,
                        alpha: float, min_names: int) -> list[dict]:
    """仅保存有限模型的滚动权重，不同时物化所有子集全截面预测。"""
    from factor_miner.research_incremental import fit_purged_ridge_models
    days = panel['date'].unique().sort().to_list()
    first = next((i for i, d in enumerate(days) if d >= evaluation_start), len(days))
    if first < train_signals or first == len(days) or alpha <= 0 or retrain_signals < 1:
        raise ValueError('冻结训练历史不足或模型参数非法')
    active = {m: f for m, f in models.items() if f}
    audit = []
    if panel.filter(pl.col('date') >= evaluation_start).group_by('date').len().filter(pl.col('len') < min_names).height:
        raise ValueError('预测日共同股票池不足')
    for index in range(first, len(days), retrain_signals):
        day = days[index]
        history = panel.filter(pl.col('date').is_in(days[index-train_signals:index]))
        fitted = fit_purged_ridge_models(history, active, day, alpha)
        for model, (beta, intercept, record) in fitted.items():
            audit.append(dict(model=model, prediction_start=day, features=active[model],
                              weights=beta.tolist(), intercept=intercept, **record))
    return audit


def predict_from_audit(panel: pl.DataFrame, model: str, features: list[str], audit: list[dict],
                       evaluation_start) -> pl.DataFrame:
    """逐子集从共同面板及已冻结权重重建预测，避免全部子集的内存峰值。"""
    current = panel.filter(pl.col('date') >= evaluation_start)
    if not features:
        return current.select('date', 'asset', pl.lit(1.).alias('score'))
    records = sorted((a for a in audit if a['model'] == model), key=lambda a: a['prediction_start'])
    if not records or records[0]['prediction_start'] != current['date'].min():
        raise ValueError('预测缺少首个冻结模型')
    rows = []
    for index, record in enumerate(records):
        if record['features'] != features:
            raise ValueError('冻结模型特征顺序变化')
        frame = current.filter(pl.col('date') >= record['prediction_start'])
        if index+1 < len(records):
            frame = frame.filter(pl.col('date') < records[index+1]['prediction_start'])
        # 与逐日矩阵乘法完全相同；按原日顺序避免库批量算法的排序差异。
        for day in frame.partition_by('date', maintain_order=True):
            scores = day.select(features).to_numpy() @ np.array(record['weights']) + record['intercept']
            rows.append(day.select('date', 'asset').with_columns(pl.Series('score', scores)))
    return pl.concat(rows).sort('date', 'asset')


def rolling_predictions(panel: pl.DataFrame, models: dict[str, list[str]], **policy):
    """小型调用兼容入口；真实研究使用逐子集重建以控制内存。"""
    audit = fit_rolling_models(panel, models, **policy)
    return {model: predict_from_audit(panel, model, features, audit, policy['evaluation_start'])
            for model, features in models.items()}, audit
