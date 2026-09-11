"""将合法 typed AST 编译为确定性的 Polars 表达式计划。"""

from __future__ import annotations

from typing import Any, Collection

import polars as pl
import numpy as np
from scipy.stats import rankdata
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import sha256_json
from factor_miner.dsl import (
    CALENDAR_MONTH_LIMITS,
    canonical_ast,
    canonical_ast_hash,
    validate_ast,
)
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.schema import (
    CandidateFactorSpec,
    FactorNode,
    RegisteredCandidate,
    RegisteredTrustedCandidate,
    TrustedCandidateFactorSpec,
    registered_candidate,
    registered_trusted_candidate,
)


class CompiledFactorPlan(BaseModel):
    """不可变的候选编译计划及其可审计元数据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    ast_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_fields: tuple[str, ...]
    required_lookback: int = Field(ge=0)
    availability: str | dict[str, str]
    expression: dict[str, Any]
    expression_metadata: dict[str, Any]


def _window_salience_value(series: list[pl.Series], window: int) -> pl.Series:
    """完整收益窗口的显著性加权均值减普通均值，固定theta=.1、delta=.7。"""
    x, y = [s.cast(pl.Float64).to_numpy() for s in series]
    output = np.full(len(x), np.nan)
    if len(x) >= window:
        xw, yw = [np.lib.stride_tricks.sliding_window_view(v, window) for v in [x, y]]
        valid = np.isfinite(xw).all(axis=1) & np.isfinite(yw).all(axis=1)
        xw, yw = xw[valid], yw[valid]
        # .1是模型的固定结构参数，不是分母保护epsilon；输入为小数制简单收益。
        salience = np.abs(xw - yw) / (np.abs(xw) + np.abs(yw) + .1)
        weights = .7 ** rankdata(-salience, method="average", axis=1)
        measured = (weights * xw).sum(axis=1) / weights.sum(axis=1) - xw.mean(axis=1)
        output[np.flatnonzero(valid) + window - 1] = measured
    return pl.Series(output, nan_to_null=True)


def _window_lower_tail_overlap(series: list[pl.Series], window: int) -> pl.Series:
    """完整同窗两序列各自最差5%观察的交集占比；边界并列无法唯一识别时为空。"""
    x, y = [s.cast(pl.Float64).to_numpy() for s in series]
    output = np.full(len(x), np.nan)
    count = (window + 19) // 20
    if len(x) >= window and count < window:
        xw, yw = [np.lib.stride_tricks.sliding_window_view(v, window) for v in [x, y]]
        valid = np.isfinite(xw).all(axis=1) & np.isfinite(yw).all(axis=1)
        positions = np.flatnonzero(valid)
        xw, yw = xw[valid], yw[valid]
        xs, ys = [np.partition(w, [count - 1, count], axis=1) for w in [xw, yw]]
        unique = (xs[:, count - 1] < xs[:, count]) & (ys[:, count - 1] < ys[:, count])
        joint = ((xw <= xs[:, count - 1, None]) & (yw <= ys[:, count - 1, None])).sum(axis=1)
        output[positions[unique] + window - 1] = joint[unique] / count
    return pl.Series(output, nan_to_null=True)


def _window_lower_tail_mean(series: pl.Series, window: int) -> pl.Series:
    """完整有限窗口中最小ceil(window/20)个观察的均值，尾部比例固定5%。"""
    values = series.cast(pl.Float64).to_numpy()
    output = np.full(len(values), np.nan)
    if len(values) >= window:
        windows = np.lib.stride_tricks.sliding_window_view(values, window)
        valid = np.isfinite(windows).all(axis=1)
        count = (window + 19) // 20
        tail = np.partition(windows[valid], count - 1, axis=1)[:, :count]
        output[np.flatnonzero(valid) + window - 1] = tail.mean(axis=1)
    return pl.Series(output, nan_to_null=True)


def _window_drawdown_recovery(series: pl.Series, window: int) -> pl.Series:
    """正值序列最深相对回撤的最近谷底到窗口末端的毛恢复比。"""
    values = series.cast(pl.Float64).to_numpy()
    output = np.full(len(values), np.nan)
    if len(values) < window:
        return pl.Series(output, nan_to_null=True)
    windows = np.lib.stride_tricks.sliding_window_view(values, window)
    finite = np.isfinite(windows).all(axis=1) & (windows > 0).all(axis=1)
    positions = np.flatnonzero(finite)
    prices = windows[finite]
    if not len(positions):
        return pl.Series(output, nan_to_null=True)
    peak = np.maximum.accumulate(prices, axis=1)
    ratios = prices / peak
    # 最小谷峰比等价于最大对数回撤；并列时固定取最后一次谷底。
    trough = window - 1 - np.argmin(ratios[:, ::-1], axis=1)
    active = ratios.min(axis=1) < 1
    recovery = prices[:, -1] / prices[np.arange(len(prices)), trough]
    # 无回撤则没有可辨识谷底事件，不能用0回填成低恢复信号。
    output[positions[active] + window - 1] = recovery[active]
    return pl.Series(output, nan_to_null=True)


def _window_explained_increment(series: list[pl.Series], window: int) -> pl.Series:
    """同窗嵌套OLS的新增解释占比：(R2全-R2基准)/R2全。"""
    values = [s.cast(pl.Float64).to_numpy() for s in series]
    output = np.full(len(values[0]), np.nan)
    predictors = len(values) - 1
    if window <= predictors or len(output) < window:
        return pl.Series(output, nan_to_null=True)
    windows = np.stack([np.lib.stride_tricks.sliding_window_view(v, window) for v in values], axis=-1)
    finite = np.isfinite(windows).all(axis=(1, 2))
    positions = np.flatnonzero(finite)
    centered = windows[finite] - windows[finite].mean(axis=1, keepdims=True)
    y, x = centered[:, :, 0], centered[:, :, 1:]
    norms = np.linalg.norm(x, axis=1)
    total = np.einsum('ij,ij->i', y, y)
    usable = (norms > 0).all(axis=1) & (total > 0)
    x, y, total, positions = x[usable], y[usable], total[usable], positions[usable]
    if not len(positions):
        return pl.Series(output, nan_to_null=True)
    # 按列归一仅为数值求解，结果仍是原始同窗OLS；不作任何截面预处理。
    q, r = np.linalg.qr(x / norms[usable, None, :], mode='reduced')
    singular = np.linalg.svd(r, compute_uv=False)
    identifiable = (singular[:, -1] / singular[:, 0])**2 > 1e-12
    projected = np.einsum('ijk,ij->ik', q, y)
    explained = np.sum(projected**2, axis=1)
    valid = identifiable & (explained / total > 1e-12)
    # 嵌套正交投影确保新增解释非负，无需用裁剪或epsilon伪造R2。
    increment = np.sum(projected[:, 1:]**2, axis=1)
    output[positions[valid] + window - 1] = increment[valid] / explained[valid]
    return pl.Series(output, nan_to_null=True)


def _window_residual_std(series: list[pl.Series], window: int) -> pl.Series:
    """同一完整窗口带截距一元OLS，返回残差样本标准差，分母window-1。"""
    y, x = [s.cast(pl.Float64).to_numpy() for s in series]
    output = np.full(len(y), np.nan)
    if window < 2 or len(y) < window:
        return pl.Series(output, nan_to_null=True)
    yw, xw = [np.lib.stride_tricks.sliding_window_view(v, window) for v in [y, x]]
    valid = np.isfinite(yw).all(axis=1) & np.isfinite(xw).all(axis=1)
    valid &= np.ptp(xw, axis=1) > 0
    positions = np.flatnonzero(valid)
    yc = yw[valid] - yw[valid].mean(axis=1, keepdims=True)
    xc = xw[valid] - xw[valid].mean(axis=1, keepdims=True)
    slope = np.einsum('ij,ij->i', yc, xc) / np.einsum('ij,ij->i', xc, xc)
    residual = yc - slope[:, None] * xc
    measured = np.std(residual, axis=1, ddof=1)
    output[positions + window - 1] = np.where(np.isfinite(measured), measured, np.nan)
    return pl.Series(output, nan_to_null=True)


def _window_residual_last(series: list[pl.Series], window: int) -> pl.Series:
    """同一完整窗口带截距一元OLS，返回最后一个观察的有符号残差。"""
    y, x = [s.cast(pl.Float64).to_numpy() for s in series]
    output = np.full(len(y), np.nan)
    if window < 2 or len(y) < window:
        return pl.Series(output, nan_to_null=True)
    yw, xw = [np.lib.stride_tricks.sliding_window_view(v, window) for v in [y, x]]
    valid = np.isfinite(yw).all(axis=1) & np.isfinite(xw).all(axis=1)
    valid &= np.ptp(xw, axis=1) > 0
    positions = np.flatnonzero(valid)
    yc = yw[valid] - yw[valid].mean(axis=1, keepdims=True)
    xc = xw[valid] - xw[valid].mean(axis=1, keepdims=True)
    slope = np.einsum('ij,ij->i', yc, xc) / np.einsum('ij,ij->i', xc, xc)
    residual = yc - slope[:, None] * xc
    measured = residual[:, -1]
    output[positions + window - 1] = np.where(np.isfinite(measured), measured, np.nan)
    return pl.Series(output, nan_to_null=True)


def _compiler_error(code: FailureCode, message: str) -> None:
    """抛出编译阶段的稳定错误。

    参数：
        code: 编译失败代码。
        message: 中文失败说明。

    返回：
        无。该函数始终抛出 `FactorMinerError`。
    """

    raise FactorMinerError(code, message)


def _node_period(node: FactorNode) -> int:
    """读取已校验时序节点的周期参数。

    参数：
        node: delay 或 delta 节点。

    返回：
        非负周期整数。
    """

    period = node.period if node.period is not None else node.window
    if period is None or period < 0:
        _compiler_error(FailureCode.DSL_TYPE_ERROR, "时序节点缺少合法周期")
    return period


def _rolling_window(node: FactorNode) -> int:
    """读取已校验 rolling 节点的窗口参数。

    参数：
        node: rolling 节点。

    返回：
        正整数窗口。
    """

    if node.window is None or node.window <= 0:
        _compiler_error(FailureCode.DSL_TYPE_ERROR, "rolling 节点缺少合法窗口")
    if node.center is True:
        _compiler_error(FailureCode.LOOKAHEAD_DETECTED, "禁止编译 centered rolling")
    return node.window


def _safe_division(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    """构造除数为零时返回 null 的安全除法表达式。

    参数：
        numerator: 分子表达式。
        denominator: 分母表达式。

    返回：
        不添加 epsilon、除零返回 null 的 Polars 表达式。
    """

    return pl.when(denominator == 0).then(pl.lit(None)).otherwise(numerator / denominator)


def _latest_window_argmax(series: pl.Series, window: int) -> pl.Series:
    """完整有限观察窗口内最后一次最大值的位置，最早为0，最新为window-1。"""
    values = series.cast(pl.Float64).to_numpy()
    output = np.full(len(values), np.nan)
    if len(values) >= window:
        windows = np.lib.stride_tricks.sliding_window_view(values, window)
        valid = np.isfinite(windows).all(axis=1)
        positions = window - 1 - np.argmax(windows[:, ::-1], axis=1)
        output[window - 1:] = np.where(valid, positions, np.nan)
    return pl.Series(output, nan_to_null=True)


def _window_time_correlation(series: pl.Series, window: int) -> pl.Series:
    """与从旧到新观察位置的Pearson相关；完整有限窗口，常量序列为空。"""
    values = series.cast(pl.Float64).to_numpy()
    output = np.full(len(values), np.nan)
    if len(values) >= window:
        windows = np.lib.stride_tricks.sliding_window_view(values, window)
        finite = np.isfinite(windows).all(axis=1)
        usable = np.flatnonzero(finite)
        if len(usable):
            selected = windows[usable]
            variable = np.ptp(selected, axis=1) > 0
            selected = selected[variable]
            usable = usable[variable]
            centered = selected - selected.mean(axis=1, keepdims=True)
            time = np.arange(window, dtype=float) - (window - 1) / 2
            numerator = centered @ time
            denominator = np.sqrt((centered * centered).sum(axis=1) * (time * time).sum())
            with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                result = numerator / denominator
            output[usable + window - 1] = np.where(np.isfinite(result), result, np.nan)
    return pl.Series(output, nan_to_null=True)


def build_polars_expr(node: FactorNode) -> pl.Expr:
    """将单个白名单 AST 节点映射为 Polars 表达式。

    参数：
        node: 已通过 DSL 校验的 FactorNode。

    返回：
        可应用于按 asset/date 排序面板的 Polars 表达式。

    异常：
        节点算子或参数不属于白名单时抛出 `FactorMinerError`。
    """

    if node.op == "field":
        if node.field is None:
            _compiler_error(FailureCode.DSL_TYPE_ERROR, "field 节点缺少字段名")
        return pl.col(node.field)
    if node.op == "const":
        if node.value is None:
            _compiler_error(FailureCode.DSL_TYPE_ERROR, "const 节点缺少数值")
        return pl.lit(node.value)

    child_expressions = [build_polars_expr(child) for child in node.args]

    if node.op == "hlc_spread":
        previous_close, previous_high, previous_low, high, low = child_expressions
        valid = pl.all_horizontal(*(value.is_finite() & (value > 0) for value in child_expressions))
        valid = valid & (previous_high > previous_low) & (high > low) & previous_close.is_between(previous_low, previous_high)
        # 用价格比取对数保持股份单位不变；两个括号各是两倍收盘对中点的距离。
        left = (previous_close / previous_high).log() + (previous_close / previous_low).log()
        right = (previous_close / high).log() + (previous_close / low).log()
        squared = left * right
        # 仅对已知的负估计做论文两日修正；缺失和无效价格不能被max变成零。
        measured = squared.clip(lower_bound=0).sqrt()
        return pl.when(valid & squared.is_finite()).then(measured).otherwise(pl.lit(None))
    if node.op == "gt":
        left, right = child_expressions
        # 严格价格事件，等值为已知0；任何非有限输入均不能伪装为未发生事件。
        return pl.when(left.is_finite() & right.is_finite()).then((left > right).cast(pl.Float64)).otherwise(pl.lit(None))
    if node.op == "add":
        return child_expressions[0] + child_expressions[1]
    if node.op == "sub":
        return child_expressions[0] - child_expressions[1]
    if node.op == "mul":
        return child_expressions[0] * child_expressions[1]
    if node.op == "div":
        return _safe_division(child_expressions[0], child_expressions[1])
    if node.op == "neg":
        return -child_expressions[0]
    if node.op == "abs":
        return child_expressions[0].abs()
    if node.op == "sign":
        # 有效零值表示平盘；缺失与非有限输入不能变成方向信号。
        child = child_expressions[0]
        return pl.when(child.is_finite()).then(child.sign()).otherwise(pl.lit(None))
    if node.op in {"calendar_delay", "calendar_delta", "calendar_month_delay"}:
        if len(node.args) != 1 or node.args[0].op != "field":
            _compiler_error(FailureCode.DSL_TYPE_ERROR, f"{node.op} 只支持原始字段")
        # 在每个证券组中精确定位市场日历序号；缺少指定端点就返回 null，不向前找最近报价。
        sessions = pl.col("__market_session").cast(pl.Int64)
        target = (pl.col(f"__month_session_{_node_period(node)}") if node.op == "calendar_month_delay"
                  else sessions - _node_period(node))
        position = sessions.search_sorted(target).clip(0, pl.len() - 1)
        delayed = pl.when(sessions.gather(position) == target).then(
            child_expressions[0].gather(position)
        ).otherwise(pl.lit(None)).over("asset")
        return child_expressions[0] - delayed if node.op == "calendar_delta" else delayed
    if node.op == "delay":
        return child_expressions[0].shift(_node_period(node)).over("asset")
    if node.op == "delta":
        return child_expressions[0].diff(_node_period(node)).over("asset")

    window = _rolling_window(node) if node.op.startswith("rolling_") else None
    if node.op == "rolling_drawdown_recovery":
        return child_expressions[0].map_batches(
            lambda series: _window_drawdown_recovery(series, window),
            return_dtype=pl.Float64, is_elementwise=False,
        ).over("asset")
    if node.op == "rolling_argmax":
        return child_expressions[0].map_batches(
            lambda series: _latest_window_argmax(series, window),
            return_dtype=pl.Float64, is_elementwise=False,
        ).over("asset")
    if node.op == "rolling_time_corr":
        return child_expressions[0].map_batches(
            lambda series: _window_time_correlation(series, window),
            return_dtype=pl.Float64, is_elementwise=False,
        ).over("asset")
    if node.op == "rolling_sum":
        return child_expressions[0].rolling_sum(
            window_size=window, min_samples=window
        ).over("asset")
    if node.op == "rolling_mean":
        return child_expressions[0].rolling_mean(
            window_size=window, min_samples=window
        ).over("asset")
    if node.op == "rolling_std":
        return child_expressions[0].rolling_std(
            window_size=window, min_samples=window
        ).over("asset")
    if node.op == "rolling_lower_tail_mean":
        return child_expressions[0].map_batches(
            lambda series: _window_lower_tail_mean(series, window),
            return_dtype=pl.Float64, is_elementwise=False,
        ).over("asset")
    if node.op == "rolling_residual_last":
        return pl.map_batches(child_expressions, lambda values: _window_residual_last(values, window),
                              return_dtype=pl.Float64, is_elementwise=False).over("asset")
    if node.op == "rolling_salience_value":
        return pl.map_batches(child_expressions, lambda values: _window_salience_value(values, window),
                              return_dtype=pl.Float64, is_elementwise=False).over("asset")
    if node.op == "rolling_lower_tail_overlap":
        return pl.map_batches(child_expressions, lambda values: _window_lower_tail_overlap(values, window),
                              return_dtype=pl.Float64, is_elementwise=False).over("asset")
    if node.op == "rolling_negative_semibeta":
        y, x = child_expressions
        complete = y.is_finite() & x.is_finite()
        numerator = pl.when(complete).then(pl.min_horizontal(y, pl.lit(0.)) * pl.min_horizontal(x, pl.lit(0.))).otherwise(pl.lit(None))
        denominator = pl.when(complete).then(x * x).otherwise(pl.lit(None))
        measured = _safe_division(
            numerator.rolling_sum(window_size=window, min_samples=window).over("asset"),
            denominator.rolling_sum(window_size=window, min_samples=window).over("asset"),
        )
        return pl.when(measured.is_finite()).then(measured).otherwise(pl.lit(None))
    if node.op == "rolling_residual_std":
        return pl.map_batches(child_expressions, lambda values: _window_residual_std(values, window),
                              return_dtype=pl.Float64, is_elementwise=False).over("asset")
    if node.op == "rolling_explained_increment":
        return pl.map_batches(child_expressions, lambda values: _window_explained_increment(values, window),
                              return_dtype=pl.Float64, is_elementwise=False).over("asset")
    if node.op == "rolling_skew":
        # 固定使用调整后的Fisher-Pearson样本偏度；完整有限窗口，常量窗口无定义。
        child = child_expressions[0]
        finite = pl.when(child.is_finite()).then(child).otherwise(pl.lit(None))
        skew = finite.rolling_skew(window_size=window, bias=False, min_samples=window, center=False).over("asset")
        variance = finite.rolling_var(window_size=window, min_samples=window).over("asset")
        return pl.when((variance > 0) & skew.is_finite()).then(skew).otherwise(pl.lit(None))
    if node.op == "rolling_min":
        return child_expressions[0].rolling_min(
            window_size=window, min_samples=window
        ).over("asset")
    if node.op == "rolling_max":
        return child_expressions[0].rolling_max(
            window_size=window, min_samples=window
        ).over("asset")
    if node.op == "rolling_cov":
        # 标准样本协方差：共同完整窗口，非有限值不可成为有效观测，常数协方差为零。
        complete = pl.all_horizontal(*(value.is_finite() for value in child_expressions))
        left, right = [pl.when(complete).then(value).otherwise(pl.lit(None)) for value in child_expressions]
        return pl.rolling_cov(left, right, window_size=window, min_samples=window, ddof=1).over("asset")
    if node.op == "rolling_corr":
        return pl.rolling_corr(
            child_expressions[0],
            child_expressions[1],
            window_size=window,
            min_samples=window,
        ).over("asset")
    if node.op == "rolling_partial_corr":
        # 三个序列使用完全相同的完整窗口；剔除控制变量的线性影响。
        complete = pl.all_horizontal(*(value.is_finite() for value in child_expressions))
        x, y, z = [pl.when(complete).then(value).otherwise(pl.lit(None)) for value in child_expressions]
        def correlation(left: pl.Expr, right: pl.Expr) -> pl.Expr:
            return pl.rolling_corr(left, right, window_size=window, min_samples=window).over("asset")
        xy, xz, yz = correlation(x, y), correlation(x, z), correlation(y, z)
        residual_x, residual_y = 1 - xz * xz, 1 - yz * yz
        value = (xy - xz * yz) / (residual_x * residual_y).sqrt()
        # 残差比例过小则无法可靠识别偏相关，返回 null，不以 epsilon 制造数值。
        admissible = (residual_x > 1e-12) & (residual_y > 1e-12) & value.is_finite() & (value.abs() <= 1 + 1e-10)
        return pl.when(admissible).then(value.clip(-1., 1.)).otherwise(pl.lit(None))
    if node.op == "rolling_partial_beta":
        # x对y的斜率，控制截距及一到两个变量；所有输入共享完整窗口。
        complete = pl.all_horizontal(*(value.is_finite() for value in child_expressions))
        values = [pl.when(complete).then(value).otherwise(pl.lit(None)) for value in child_expressions]
        x, y, z = values[:3]
        def covariance(left: pl.Expr, right: pl.Expr) -> pl.Expr:
            return pl.rolling_cov(left, right, window_size=window, min_samples=window).over("asset")
        def variance(value: pl.Expr) -> pl.Expr:
            return value.rolling_var(window_size=window, min_samples=window).over("asset")
        variance_y, variance_z = variance(y), variance(z)
        yz = covariance(y, z)
        if len(values) == 3:
            # 保留既有三参数的计算次序，历史候选重算语义不变。
            residual_y = variance_y - yz * yz / variance_z
            slope = (covariance(x, y) - covariance(x, z) * yz / variance_z) / residual_y
            controls_valid = variance_z > 0
        else:
            w = values[3]
            variance_w, zw, yw = variance(w), covariance(z, w), covariance(y, w)
            determinant = variance_z * variance_w - zw * zw
            coefficient_z = (yz * variance_w - yw * zw) / determinant
            coefficient_w = (yw * variance_z - yz * zw) / determinant
            residual_y = variance_y - coefficient_z * yz - coefficient_w * yw
            slope = (covariance(x, y) - coefficient_z * covariance(x, z) - coefficient_w * covariance(x, w)) / residual_y
            controls_valid = (variance_z > 0) & (variance_w > 0) & (determinant / (variance_z * variance_w) > 1e-12)
        admissible = controls_valid & (variance_y > 0) & (residual_y / variance_y > 1e-12) & slope.is_finite()
        return pl.when(admissible).then(slope).otherwise(pl.lit(None))

    _compiler_error(FailureCode.DSL_TYPE_ERROR, f"未知编译算子：{node.op}")


def attach_market_sessions(frame: pl.LazyFrame, calendar: pl.DataFrame, *, calendar_months: bool = False) -> pl.LazyFrame:
    """为固定日历算子附加显式日历序号，不新增证券行情行或填补价格。

    参数：frame 为原始市场面板，calendar 必须含完整、唯一、非空的 Date 类型 date 列。
    返回：保持原始行及字段并按日期排序的面板；日期不在日历或主键重复时失败。
    """
    if any(name == "__market_session" or name.startswith("__month_session_") for name in frame.collect_schema()):
        raise ValueError("市场面板不得自行提供保留的日历序号")
    dates = calendar.select("date")
    if dates.schema["date"] != pl.Date or dates.is_empty() or dates["date"].null_count():
        raise ValueError("市场日历必须包含非空 Date 日期")
    if dates["date"].n_unique() != dates.height:
        raise ValueError("市场日历日期重复")
    if frame.select(pl.struct("date", "asset").is_duplicated().any()).collect().item():
        raise ValueError("市场面板主键重复")
    if frame.select(pl.any_horizontal(pl.col("date").is_null(), pl.col("asset").is_null()).any()).collect().item():
        raise ValueError("市场面板主键缺失")
    if frame.select("date").unique().join(dates.lazy(), on="date", how="anti").limit(1).collect().height:
        raise ValueError("市场面板存在日历外日期")
    indexed = dates.sort("date").with_row_index("__market_session").with_columns(pl.col("__market_session").cast(pl.Int64))
    if calendar_months:
        # 先按完整市场日历确定月末；不能按每只证券剩余行或交易日近似月份。
        day_list = indexed["date"].to_list()
        month_ends = {day.year * 12 + day.month - 1: i for i, day in enumerate(day_list)}
        indexed = indexed.with_columns([
            pl.Series(f"__month_session_{period}",
                      [month_ends.get(day.year * 12 + day.month - 1 - period) for day in day_list],
                      dtype=pl.Int64)
            for period in CALENDAR_MONTH_LIMITS.calendar_month_periods
        ])
    return frame.join(indexed.lazy(), on="date", how="left", validate="m:1").sort("date", "asset")


def _candidate_record(
    candidate: RegisteredCandidate
    | CandidateFactorSpec
    | RegisteredTrustedCandidate
    | TrustedCandidateFactorSpec,
) -> RegisteredCandidate | RegisteredTrustedCandidate:
    """将候选输入统一为已登记记录。

    参数：
        candidate: 已登记候选或尚未包装的合法候选 spec。

    返回：
        可用于编译审计的 `RegisteredCandidate`。
    """

    if isinstance(candidate, (RegisteredCandidate, RegisteredTrustedCandidate)):
        return candidate
    if isinstance(candidate, TrustedCandidateFactorSpec):
        return registered_trusted_candidate(candidate)
    return registered_candidate(candidate)


def compile_candidate(
    candidate: RegisteredCandidate
    | CandidateFactorSpec
    | RegisteredTrustedCandidate
    | TrustedCandidateFactorSpec,
    allowed_fields: Collection[str],
) -> CompiledFactorPlan:
    """校验并编译候选，生成确定性 Polars 计划。

    参数：
        candidate: 已登记候选或合法候选 spec。
        allowed_fields: 当前数据合同允许的原始字段。

    返回：
        记录 AST 哈希、计划哈希、字段、lookback 和可得性的编译计划。

    异常：
        AST 非法、标签泄漏、字段声明不一致或 lookback 超限时抛出错误。
    """

    record = _candidate_record(candidate)
    month_clock = record.spec.spec_version == "3"
    # 带新合同的候选无论由 API 还是人工提交，都不能绕过同一个 Γ 验证器。
    gamma_json = record.spec.provenance.get("gamma_json") if hasattr(record.spec, "provenance") else None
    if hasattr(record.spec, "provenance") and record.spec.provenance.get("favor_plan_sha256") and not gamma_json:
        raise ValueError("FaVOR 候选缺少必需的冻结 Γ")
    if gamma_json:
        from factor_miner.hypothesis_constraints import CompiledGamma
        gamma = CompiledGamma.model_validate_json(gamma_json)
        if month_clock != (gamma.version == "hypothesis-gamma-calendar-month-v1"):
            raise ValueError("候选与 Γ 的日历观察协议不一致")
        if record.spec.provenance.get("gamma_sha256") != gamma.identity:
            raise ValueError("Γ 内容与冻结身份不一致")
        gamma.validate(record.spec.expression, record.spec.hypothesis)
    if month_clock and not gamma_json:
        raise ValueError("日历月候选必须绑定显式冻结 Γ")
    forbidden_fields = {
        "label",
        "label_o2o_1d",
        "label_o2o_5d",
        "label_o2o_20d",
        "target",
        "forward_return",
    }
    metadata = validate_ast(
        record.spec.expression,
        allowed_fields,
        forbidden_fields,
        limits=CALENDAR_MONTH_LIMITS if month_clock else None,
    )
    declared_fields = tuple(sorted(record.spec.required_fields))
    if declared_fields != metadata.required_fields:
        _compiler_error(
            FailureCode.FIELD_MISSING,
            "CandidateFactorSpec.required_fields 与 AST 实际字段不一致",
        )
    if metadata.lookback > record.spec.max_lookback:
        _compiler_error(
            FailureCode.DSL_TYPE_ERROR,
            "AST 实际 lookback 超过 CandidateFactorSpec 声明值",
        )

    expression = canonical_ast(record.spec.expression)
    expression_metadata = {
        "node_count": metadata.node_count,
        "depth": metadata.depth,
        "operator_signature": metadata.operator_signature,
        "field_signature": metadata.field_signature,
    }
    if {"calendar_delay", "calendar_delta"}.intersection(metadata.operator_signature):
        expression_metadata["required_context"] = "explicit_market_session_index_v1"
    if "calendar_month_delay" in metadata.operator_signature:
        expression_metadata["required_context"] = "explicit_market_calendar_month_v1"
        expression_metadata["lookback_semantics"] = "conservative_session_bound_31_per_month"
    ast_hash = canonical_ast_hash(record.spec.expression)
    plan_payload = {
        "candidate_id": record.candidate_id,
        "ast_hash": ast_hash,
        "required_fields": metadata.required_fields,
        "required_lookback": metadata.lookback,
        "availability": (
            record.spec.availability.model_dump(mode="json")
            if hasattr(record.spec.availability, "model_dump")
            else record.spec.availability
        ),
        "expression": expression,
        "expression_metadata": expression_metadata,
    }
    return CompiledFactorPlan(
        candidate_id=record.candidate_id,
        ast_hash=ast_hash,
        plan_hash=sha256_json(plan_payload),
        required_fields=metadata.required_fields,
        required_lookback=metadata.lookback,
        availability=(
            record.spec.availability.model_dump(mode="json")
            if hasattr(record.spec.availability, "model_dump")
            else record.spec.availability
        ),
        expression=expression,
        expression_metadata=expression_metadata,
    )
