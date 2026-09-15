"""候选筛选使用的便携滚动 Ridge 与 Top50 评价。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
import math
from pathlib import Path
from statistics import mean
from typing import Sequence

import numpy as np
import polars as pl


@dataclass(frozen=True)
class RollingRidgePolicy:
    train_weeks: int = 156
    validation_weeks: int = 26
    embargo_weeks: int = 1
    retrain_weeks: int = 4
    alphas: tuple[float, ...] = (0.1, 1.0, 10.0, 100.0)
    top_n: int = 50
    cost_per_l1_turnover: float = 0.0014

    def __post_init__(self) -> None:
        if self.train_weeks <= self.validation_weeks or self.validation_weeks < 2:
            raise ValueError("训练窗必须大于验证窗，且验证窗至少两周")
        if self.embargo_weeks < 1 or self.retrain_weeks < 1 or self.top_n < 1 or not self.alphas:
            raise ValueError("重训频率、TopN 和 alpha 网格必须为正")


def _fit_ridge(x: np.ndarray, y: np.ndarray, alpha: float) -> tuple[np.ndarray, float]:
    center_x = x.mean(axis=0)
    center_y = float(y.mean())
    xc = x - center_x
    beta = np.linalg.solve(xc.T @ xc + alpha * np.eye(x.shape[1]), xc.T @ (y - center_y))
    return beta, center_y - float(center_x @ beta)


def _mean_date_ic(frame: pl.DataFrame) -> float:
    values: list[float] = []
    for day in frame.partition_by("date", maintain_order=True):
        if day.height < 20:
            continue
        corr = day.select(pl.corr("prediction", "target_z", method="spearman")).item()
        if corr is not None and math.isfinite(float(corr)):
            values.append(float(corr))
    return mean(values) if values else -math.inf


def _matrix(frame: pl.DataFrame, features: Sequence[str]) -> tuple[np.ndarray, np.ndarray]:
    complete = frame.drop_nulls([*features, "target_z"])
    if complete.height == 0:
        raise ValueError("Ridge 训练窗口没有完整样本")
    return complete.select(features).to_numpy(), complete.get_column("target_z").to_numpy()


def rolling_ridge_predictions(
    panel: pl.DataFrame,
    features: Sequence[str],
    policy: RollingRidgePolicy = RollingRidgePolicy(),
) -> pl.DataFrame:
    """使用过去 156 周、26 周内层验证和每 4 周重训生成严格走步预测。"""

    required = {"date", "code", "label_raw", "target_z", *features}
    if missing := required.difference(panel.columns):
        raise ValueError(f"Ridge 面板缺少字段：{sorted(missing)}")
    if not features:
        raise ValueError("Ridge 特征不能为空")
    data = panel.select("date", "code", "label_raw", "target_z", *features).with_columns(
        [
            pl.when(pl.col(name).is_finite())
            .then(
                (pl.col(name) - pl.col(name).mean().over("date"))
                / pl.col(name).std(ddof=0).over("date")
            )
            .otherwise(None)
            .fill_null(0.0)
            .alias(name)
            for name in features
        ]
    ).sort(["date", "code"])
    dates = data.get_column("date").unique().sort().to_list()
    start_index = policy.train_weeks + policy.embargo_weeks
    if len(dates) <= start_index:
        raise ValueError("周数不足以启动滚动训练")
    outputs: list[pl.DataFrame] = []
    beta: np.ndarray | None = None
    intercept = 0.0
    selected_alpha = policy.alphas[0]
    for index in range(start_index, len(dates)):
        if beta is None or (index - start_index) % policy.retrain_weeks == 0:
            train_end = index - policy.embargo_weeks
            train_dates = dates[train_end - policy.train_weeks:train_end]
            split = policy.train_weeks - policy.validation_weeks
            fit = data.filter(pl.col("date").is_in(train_dates[:split]))
            valid = data.filter(pl.col("date").is_in(train_dates[split:])).drop_nulls("target_z")
            x_fit, y_fit = _matrix(fit, features)
            best = (-math.inf, policy.alphas[0])
            for alpha in policy.alphas:
                trial_beta, trial_intercept = _fit_ridge(x_fit, y_fit, alpha)
                scores = valid.select(features).to_numpy() @ trial_beta + trial_intercept
                score = _mean_date_ic(valid.select("date", "target_z").with_columns(pl.Series("prediction", scores)))
                best = max(best, (score, -alpha))
            selected_alpha = -best[1]
            x_train, y_train = _matrix(data.filter(pl.col("date").is_in(train_dates)), features)
            beta, intercept = _fit_ridge(x_train, y_train, selected_alpha)
        current = data.filter(pl.col("date") == dates[index]).drop_nulls("label_raw")
        scores = current.select(features).to_numpy() @ beta + intercept
        outputs.append(
            current.select("date", "code", "label_raw")
            .with_columns(pl.Series("prediction", scores), pl.lit(selected_alpha).alias("alpha"))
        )
    return pl.concat(outputs).sort(["date", "code"])


def top_n_metrics(predictions: pl.DataFrame, policy: RollingRidgePolicy) -> tuple[pl.DataFrame, dict[str, float | int]]:
    """把逐股票预测映射为 TopN 等权组合并计算净收益指标。"""

    predictions = predictions.filter(
        pl.col("prediction").is_finite().fill_null(False)
        & pl.col("label_raw").is_finite().fill_null(False)
    )
    weekly: list[dict[str, object]] = []
    previous: set[str] = set()
    for day in predictions.partition_by("date", maintain_order=True):
        chosen = day.sort("prediction", descending=True).head(policy.top_n)
        names = set(chosen.get_column("code").to_list())
        if not names:
            continue
        turnover = sum(abs((1 / len(names) if name in names else 0) - (1 / len(previous) if name in previous else 0)) for name in names | previous)
        gross = float(chosen.get_column("label_raw").mean())
        weekly.append({"date": chosen.item(0, "date"), "gross_return": gross, "l1_turnover": turnover, "net_return": gross - turnover * policy.cost_per_l1_turnover})
        previous = names
    result = pl.DataFrame(weekly)
    returns = [float(value) for value in result.get_column("net_return").to_list()]
    avg = mean(returns)
    std = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
    wealth = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in returns:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        drawdown = max(drawdown, 1.0 - wealth / peak)
    metrics: dict[str, float | int] = {
        "weeks": len(returns),
        "annualized_return": wealth ** (52 / len(returns)) - 1 if returns else 0.0,
        "sharpe": avg / std * math.sqrt(52) if std else 0.0,
        "max_drawdown": drawdown,
        "mean_l1_turnover": float(result.get_column("l1_turnover").mean()) if result.height else 0.0,
    }
    return result, metrics


def run_and_publish_rolling_ridge(
    *,
    panel_path: Path,
    features: Sequence[str],
    output_root: Path,
    policy: RollingRidgePolicy = RollingRidgePolicy(),
    evaluation_start: date | None = None,
    evaluation_end: date | None = None,
) -> dict[str, object]:
    """运行走步 Ridge，并把预测、逐周收益和摘要写入新的独立目录。"""

    panel_path = panel_path.expanduser().resolve(strict=True)
    output_root = output_root.expanduser().resolve(strict=False)
    if output_root.exists():
        raise ValueError("Ridge 输出目录已存在，请使用新的路径")
    output_root.mkdir(parents=True)
    panel = pl.read_parquet(panel_path)
    predictions = rolling_ridge_predictions(panel, features, policy)
    if evaluation_start is not None:
        predictions = predictions.filter(pl.col("date") >= evaluation_start)
    if evaluation_end is not None:
        predictions = predictions.filter(pl.col("date") <= evaluation_end)
    weekly, metrics = top_n_metrics(predictions, policy)
    prediction_path = output_root / "逐股票预测.parquet"
    weekly_path = output_root / "逐周收益.csv"
    predictions.write_parquet(prediction_path, compression="zstd")
    weekly.write_csv(weekly_path, include_bom=True)

    def file_hash(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    summary: dict[str, object] = {
        "version": "rolling-ridge-v1",
        "research_boundary": "已使用历史诊断区间，不是密封样本外或认证 Alpha",
        "features": list(features),
        "feature_count": len(features),
        "policy": {
            "train_weeks": policy.train_weeks,
            "validation_weeks": policy.validation_weeks,
            "embargo_weeks": policy.embargo_weeks,
            "retrain_weeks": policy.retrain_weeks,
            "alphas": list(policy.alphas),
            "validation_objective": "mean_weekly_cross_section_spearman",
            "top_n": policy.top_n,
            "cost_per_l1_turnover": policy.cost_per_l1_turnover,
        },
        "evaluation_start": evaluation_start.isoformat() if evaluation_start else None,
        "evaluation_end": evaluation_end.isoformat() if evaluation_end else None,
        "metrics": metrics,
        "source_panel_sha256": file_hash(panel_path),
        "prediction_sha256": file_hash(prediction_path),
        "weekly_returns_sha256": file_hash(weekly_path),
    }
    (output_root / "摘要.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary
