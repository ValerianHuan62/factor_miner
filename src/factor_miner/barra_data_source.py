"""服务器派生 Barra Parquet 到阶段 A 归因端口的只读适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl

from factor_miner.barra_schema import BarraEvaluationPolicy, BarraInputIdentity
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.pilot_schema import PilotSourcePaths
from factor_miner.portfolio_evaluation import PortfolioBacktestResult
from factor_miner.trading_schedule import RebalanceWindow


def _error(message: str) -> FactorMinerError:
    """构造稳定的 Barra 派生数据合同错误。"""

    return FactorMinerError(FailureCode.BARRA_DATA_CONTRACT_INVALID, message)


def _scan(path: object, name: str) -> pl.LazyFrame:
    """只读取显式 Parquet 文件或目录。"""

    if path is None:
        raise _error(f"缺少 {name}")
    source = pl.scan_parquet(path)
    try:
        source.collect_schema()
    except Exception as error:
        raise _error(f"无法读取 {name}：{error}") from error
    return source


def _require_columns(frame: pl.LazyFrame, required: set[str], name: str) -> None:
    """检查端口必需字段。"""

    missing = required.difference(frame.collect_schema().names())
    if missing:
        raise _error(f"{name} 缺少字段：{sorted(missing)}")


def _normalize_date(frame: pl.LazyFrame, name: str) -> pl.LazyFrame:
    """接受 Date 或午夜 Datetime，并统一为 Date。"""

    dtype = dict(frame.collect_schema()).get("date")
    if dtype == pl.Date:
        return frame
    if isinstance(dtype, pl.Datetime) and dtype.time_zone is None:
        if frame.filter(pl.col("date") != pl.col("date").dt.truncate("1d")).limit(1).collect().height:
            raise _error(f"{name} date 必须位于午夜")
        return frame.with_columns(pl.col("date").cast(pl.Date))
    raise _error(f"{name} date 必须是 Date 或无时区午夜 Datetime")


def _reject_invalid_keys(frame: pl.LazyFrame, keys: list[str], name: str) -> None:
    """主键必须非空且唯一。"""

    if frame.filter(pl.any_horizontal([pl.col(key).is_null() for key in keys])).limit(1).collect().height:
        raise _error(f"{name} 主键包含空值")
    if frame.group_by(keys).len().filter(pl.col("len") > 1).limit(1).collect().height:
        raise _error(f"{name} 主键重复：{keys}")


@dataclass(frozen=True, slots=True)
class DerivedBarraInputs:
    """已经按调仓窗口规范化的三个 Barra 归因端口。"""

    exposures: pl.LazyFrame
    exposure_asof: pl.LazyFrame
    factor_returns: pl.LazyFrame
    benchmark_weights: pl.LazyFrame
    covariance: pl.LazyFrame | None
    specific_risk: pl.LazyFrame | None
    identity: BarraInputIdentity


def _asof_by_schedule(
    source: pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
    *,
    value_columns: list[str],
    name: str,
    max_staleness_days: int,
    required_securities: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame, date]:
    """为每个信号日的当期成分选择最近且不晚于该日的快照。"""

    required = required_securities.select(
        ["signal_date", "security_id"]
    ).unique().sort(["security_id", "signal_date"])
    if required.is_empty():
        raise _error("Barra 调仓窗口不能为空")
    security_ids = required.get_column("security_id").unique().to_list()
    maximum_signal = required.get_column("signal_date").max()
    history = (
        source.filter(
            (pl.col("date") <= maximum_signal)
            & pl.col("security_id").is_in(security_ids)
        )
        .select(["date", "security_id", *value_columns])
        .sort(["security_id", "date"])
        .collect()
    )
    aligned = required.join_asof(
        history,
        left_on="signal_date",
        right_on="date",
        by="security_id",
        strategy="backward",
        check_sortedness=False,
    )
    if aligned.filter(pl.col("date").is_null()).height:
        raise _error(f"{name} 缺少信号日当日或更早数据")
    if aligned.filter(pl.col("date") > pl.col("signal_date")).height:
        raise _error(f"{name} 发生未来信息对齐")
    if aligned.filter(
        (pl.col("signal_date") - pl.col("date")).dt.total_days()
        > max_staleness_days
    ).height:
        raise _error(f"{name} 超过 {max_staleness_days} 个自然日陈旧阈值")
    selected_asof = aligned.get_column("date").max()
    if not isinstance(selected_asof, date):
        raise _error(f"{name} 没有有效可得日")
    return (
        aligned.select(["signal_date", "security_id", *value_columns]),
        aligned.select(
            [
                "signal_date",
                "security_id",
                pl.col("date").alias("exposure_asof_date"),
            ]
        ),
        selected_asof,
    )


def _all_security_snapshots(
    source: pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
    *,
    value_columns: list[str],
    name: str,
    max_staleness_days: int,
) -> tuple[pl.DataFrame, pl.DataFrame, date]:
    """研究池未单列时，按全局最近快照保留当日全部证券。"""

    periods = pl.DataFrame(
        {"signal_date": [window.signal_date for window in schedule]}
    ).sort("signal_date")
    available_dates = (
        source.filter(pl.col("date") <= periods.get_column("signal_date").max())
        .select("date")
        .unique()
        .sort("date")
        .collect()
    )
    snapshots = periods.join_asof(
        available_dates,
        left_on="signal_date",
        right_on="date",
        strategy="backward",
    )
    if snapshots.filter(pl.col("date").is_null()).height:
        raise _error(f"{name} 缺少信号日当日或更早快照")
    if snapshots.filter(
        (pl.col("signal_date") - pl.col("date")).dt.total_days()
        > max_staleness_days
    ).height:
        raise _error(f"{name} 超过 {max_staleness_days} 个自然日陈旧阈值")
    selected_dates = snapshots.get_column("date").unique().to_list()
    values = source.filter(pl.col("date").is_in(selected_dates)).collect()
    aligned = snapshots.join(values, on="date", how="inner")
    selected_asof = aligned.get_column("date").max()
    if not isinstance(selected_asof, date):
        raise _error(f"{name} 没有有效可得日")
    return (
        aligned.select(["signal_date", "security_id", *value_columns]),
        aligned.select(
            [
                "signal_date",
                "security_id",
                pl.col("date").alias("exposure_asof_date"),
            ]
        ),
        selected_asof,
    )


def _benchmark_snapshots(
    source: pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
) -> pl.DataFrame:
    """按每个信号日选一个全局 CSI300 权重快照及其全部成分。"""

    if not schedule:
        raise _error("Barra 调仓窗口不能为空")
    periods = pl.DataFrame(
        {
            "signal_date": [window.signal_date for window in schedule],
            "entry_date": [window.entry_date for window in schedule],
        }
    ).sort("signal_date")
    available_dates = (
        source.filter(pl.col("date") <= periods.get_column("signal_date").max())
        .select("date")
        .unique()
        .sort("date")
        .collect()
    )
    snapshots = periods.join_asof(
        available_dates,
        left_on="signal_date",
        right_on="date",
        strategy="backward",
    )
    if snapshots.filter(pl.col("date").is_null()).height:
        raise _error("CSI300 权重缺少信号日当日或更早快照")
    snapshot_dates = snapshots.get_column("date").unique().to_list()
    weights = source.filter(pl.col("date").is_in(snapshot_dates)).collect()
    return (
        snapshots.join(weights, on="date", how="inner")
        .select(["signal_date", "entry_date", "security_id", "weight"])
        .sort(["signal_date", "security_id"])
    )


def _universe_snapshots(
    paths: PilotSourcePaths,
    schedule: tuple[RebalanceWindow, ...],
) -> pl.DataFrame:
    """读取每个信号日研究池成分，用于覆盖基准外组合持仓。"""

    if paths.universe_uri is None:
        return pl.DataFrame(
            schema={"signal_date": pl.Date, "security_id": pl.String}
        )
    source = _normalize_date(
        _scan(paths.universe_uri, "universe_uri"),
        "研究池成分",
    )
    columns = set(source.collect_schema().names())
    security_column = next(
        (
            name
            for name in ("security_id", "order_book_id", "code")
            if name in columns
        ),
        None,
    )
    if security_column is None:
        raise _error("研究池成分缺少 security_id/order_book_id/code")
    periods = pl.DataFrame(
        {"signal_date": [window.signal_date for window in schedule]}
    ).sort("signal_date")
    available_dates = (
        source.filter(pl.col("date") <= periods.get_column("signal_date").max())
        .select("date")
        .unique()
        .sort("date")
        .collect()
    )
    snapshots = periods.join_asof(
        available_dates,
        left_on="signal_date",
        right_on="date",
        strategy="backward",
    )
    if snapshots.filter(pl.col("date").is_null()).height:
        raise _error("研究池成分缺少信号日当日或更早快照")
    dates = snapshots.get_column("date").unique().to_list()
    members = (
        source.filter(pl.col("date").is_in(dates))
        .select("date", pl.col(security_column).alias("security_id"))
        .collect()
    )
    return (
        snapshots.join(members, on="date", how="inner")
        .select("signal_date", "security_id")
        .unique()
        .sort(["signal_date", "security_id"])
    )


def _covariance_snapshots(
    source: pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
) -> pl.DataFrame:
    """为每个信号日选择一份全局最近协方差矩阵。"""

    periods = pl.DataFrame(
        {"signal_date": [window.signal_date for window in schedule]}
    ).sort("signal_date")
    available_dates = (
        source.filter(pl.col("date") <= periods.get_column("signal_date").max())
        .select("date")
        .unique()
        .sort("date")
        .collect()
    )
    snapshots = periods.join_asof(
        available_dates,
        left_on="signal_date",
        right_on="date",
        strategy="backward",
    )
    if snapshots.filter(pl.col("date").is_null()).height:
        raise _error("Barra 协方差缺少信号日当日或更早快照")
    covariance = source.filter(
        pl.col("date").is_in(snapshots.get_column("date").unique().to_list())
    ).collect()
    return (
        snapshots.join(covariance, on="date", how="inner")
        .select(["signal_date", "factor_a", "factor_b", "covariance"])
        .sort(["signal_date", "factor_a", "factor_b"])
    )


def load_derived_barra_inputs(
    paths: PilotSourcePaths,
    schedule: tuple[RebalanceWindow, ...],
    policy: BarraEvaluationPolicy,
) -> DerivedBarraInputs:
    """加载显式派生 Barra 数据并转换为周度归因合同。

    暴露和指数权重只取信号日或更早最近快照；日频因子收益在
    ``[entry_date, exit_date)`` 内几何复合，避免读入退出日之后的信息。
    """

    if not schedule:
        raise _error("Barra 调仓窗口不能为空")
    staleness_days = paths.barra_max_exposure_staleness_days
    if staleness_days is None:
        raise _error("缺少显式 Barra 暴露陈旧阈值")
    exposure = _normalize_date(
        _scan(paths.barra_exposure_uri, "barra_exposure_uri"),
        "Barra 暴露",
    )
    daily_returns = _normalize_date(
        _scan(paths.barra_factor_returns_uri, "barra_factor_returns_uri"),
        "Barra 因子收益",
    )
    benchmark = _normalize_date(
        _scan(paths.barra_benchmark_weights_uri, "barra_benchmark_weights_uri"),
        "CSI300 权重",
    )
    _require_columns(exposure, {"date", "security_id"}, "Barra 暴露")
    _require_columns(daily_returns, {"date", "factor", "factor_return"}, "Barra 因子收益")
    _require_columns(benchmark, {"date", "security_id", "weight"}, "CSI300 权重")
    _reject_invalid_keys(exposure, ["date", "security_id"], "Barra 暴露")
    _reject_invalid_keys(daily_returns, ["date", "factor"], "Barra 因子收益")
    _reject_invalid_keys(benchmark, ["date", "security_id"], "CSI300 权重")

    exposure_columns = [
        column
        for column in exposure.collect_schema().names()
        if column not in {"date", "security_id"}
    ]
    if not exposure_columns:
        raise _error("Barra 暴露没有行业或风格字段")
    missing_styles = set(policy.required_style_factors).difference(exposure_columns)
    if missing_styles:
        raise _error(f"Barra 暴露缺少冻结风格字段：{sorted(missing_styles)}")
    if not set(exposure_columns).difference(policy.required_style_factors):
        raise _error("Barra 暴露缺少行业字段")
    invalid_exposure = exposure.filter(
        pl.any_horizontal([pl.col(column).is_null() for column in exposure_columns])
    ).limit(1).collect()
    if invalid_exposure.height:
        raise _error("Barra 暴露包含空值")

    normalized_weights = _benchmark_snapshots(benchmark, schedule)
    invalid_weights = normalized_weights.filter(
        pl.col("weight").is_null() | (pl.col("weight") < 0)
    )
    if invalid_weights.height:
        raise _error("CSI300 权重包含空值或负值")
    weight_sums = normalized_weights.group_by("signal_date").agg(
        pl.col("weight").sum().alias("weight_sum")
    )
    if weight_sums.filter((pl.col("weight_sum") - 1.0).abs() > 1e-6).height:
        raise _error("CSI300 权重在某个信号日之和不为 1")

    required_securities = pl.concat(
        [
            normalized_weights.select(["signal_date", "security_id"]),
            _universe_snapshots(paths, schedule),
        ],
        how="vertical",
    ).unique()
    if paths.universe_uri is None:
        required_exposures, required_asof_rows, required_asof = _asof_by_schedule(
            exposure,
            schedule,
            value_columns=exposure_columns,
            name="Barra 暴露",
            max_staleness_days=staleness_days,
            required_securities=required_securities,
        )
        market_exposures, market_asof_rows, market_asof = _all_security_snapshots(
            exposure,
            schedule,
            value_columns=exposure_columns,
            name="Barra 暴露",
            max_staleness_days=staleness_days,
        )
        normalized_exposures = pl.concat(
            [required_exposures, market_exposures], how="vertical"
        ).unique(subset=["signal_date", "security_id"], keep="first")
        exposure_asof_rows = pl.concat(
            [required_asof_rows, market_asof_rows], how="vertical"
        ).unique(subset=["signal_date", "security_id"], keep="first")
        exposure_asof = max(required_asof, market_asof)
    else:
        normalized_exposures, exposure_asof_rows, exposure_asof = _asof_by_schedule(
            exposure,
            schedule,
            value_columns=exposure_columns,
            name="Barra 暴露",
            max_staleness_days=staleness_days,
            required_securities=required_securities,
        )
    missing_coverage = (
        required_securities.select(["signal_date", "security_id"])
        .join(
            normalized_exposures.select(["signal_date", "security_id"]),
            on=["signal_date", "security_id"],
            how="anti",
        )
    )
    if missing_coverage.height:
        raise _error("当前 CSI300 成分缺少信号日或更早的 Barra 暴露")
    entry_map = pl.DataFrame(
        {
            "entry_date": [window.entry_date for window in schedule],
            "exit_date": [window.exit_date for window in schedule],
        }
    ).sort("entry_date")
    daily_frame = (
        daily_returns.filter(
            (pl.col("date") >= entry_map.get_column("entry_date").min())
            & (pl.col("date") < entry_map.get_column("exit_date").max())
        )
        .collect()
        .sort("date")
    )
    weekly = (
        daily_frame.join_asof(
            entry_map,
            left_on="date",
            right_on="entry_date",
            strategy="backward",
        )
        .filter(pl.col("date") < pl.col("exit_date"))
        .group_by(["entry_date", "factor"])
        .agg(
            ((pl.col("factor_return") + 1.0).product() - 1.0).alias(
                "factor_return"
            )
        )
        .sort(["entry_date", "factor"])
    )
    actual_entries = set(weekly.get_column("entry_date").to_list())
    missing_entries = set(entry_map.get_column("entry_date").to_list()).difference(
        actual_entries
    )
    if missing_entries:
        raise _error(f"Barra 因子收益缺少持有区间：{min(missing_entries)}")
    expected_factors = set(exposure_columns)
    for entry_date in entry_map.get_column("entry_date"):
        actual = set(weekly.filter(pl.col("entry_date") == entry_date)["factor"].to_list())
        if actual != expected_factors:
            raise _error(f"Barra 因子收益字段与暴露不一致：{entry_date}")

    max_signal = max(window.signal_date for window in schedule)
    covariance_output: pl.LazyFrame | None = None
    specific_risk_output: pl.LazyFrame | None = None
    if policy.attribution_mode == "full_risk_decomposition":
        if paths.barra_covariance_uri is None or paths.barra_specific_risk_uri is None:
            raise _error("完整 Barra 风险分解缺少协方差或特异风险 URI")
        covariance_source = _normalize_date(
            _scan(paths.barra_covariance_uri, "barra_covariance_uri"),
            "Barra 协方差",
        )
        specific_source = _normalize_date(
            _scan(paths.barra_specific_risk_uri, "barra_specific_risk_uri"),
            "Barra 特异风险",
        )
        _require_columns(
            covariance_source,
            {"date", "factor_a", "factor_b", "covariance"},
            "Barra 协方差",
        )
        _require_columns(
            specific_source,
            {"date", "security_id", "specific_risk"},
            "Barra 特异风险",
        )
        _reject_invalid_keys(
            covariance_source,
            ["date", "factor_a", "factor_b"],
            "Barra 协方差",
        )
        _reject_invalid_keys(
            specific_source,
            ["date", "security_id"],
            "Barra 特异风险",
        )
        covariance_output = _covariance_snapshots(
            covariance_source,
            schedule,
        ).sort(["signal_date", "factor_a", "factor_b"]).lazy()
        if paths.universe_uri is None:
            required_specific, _, _ = _asof_by_schedule(
                specific_source,
                schedule,
                value_columns=["specific_risk"],
                name="Barra 特异风险",
                max_staleness_days=staleness_days,
                required_securities=required_securities,
            )
            market_specific, _, _ = _all_security_snapshots(
                specific_source,
                schedule,
                value_columns=["specific_risk"],
                name="Barra 特异风险",
                max_staleness_days=staleness_days,
            )
            normalized_specific = pl.concat(
                [required_specific, market_specific], how="vertical"
            ).unique(subset=["signal_date", "security_id"], keep="first")
        else:
            normalized_specific, _, _ = _asof_by_schedule(
                specific_source,
                schedule,
                value_columns=["specific_risk"],
                name="Barra 特异风险",
                max_staleness_days=staleness_days,
                required_securities=required_securities,
            )
        missing_specific = (
            required_securities.select(["signal_date", "security_id"])
            .join(
                normalized_specific.select(["signal_date", "security_id"]),
                on=["signal_date", "security_id"],
                how="anti",
            )
        )
        if missing_specific.height:
            raise _error("当前 CSI300 成分缺少信号日或更早的特异风险")
        specific_risk_output = normalized_specific.sort(
            ["signal_date", "security_id"]
        ).lazy()
    identity = BarraInputIdentity(
        exposure_version=policy.exposure_version,
        factor_return_version=policy.factor_return_version,
        covariance_version=policy.covariance_version,
        specific_risk_version=policy.specific_risk_version,
        exposure_asof_date=exposure_asof,
        signal_date=max_signal,
        industry_columns=tuple(sorted(set(exposure_columns).difference(policy.required_style_factors))),
        style_columns=tuple(sorted(policy.required_style_factors)),
    )
    return DerivedBarraInputs(
        exposures=normalized_exposures.sort(["signal_date", "security_id"]).lazy(),
        exposure_asof=exposure_asof_rows.sort(["signal_date", "security_id"]).lazy(),
        factor_returns=weekly.lazy(),
        benchmark_weights=normalized_weights.sort(["signal_date", "security_id"]).lazy(),
        covariance=covariance_output,
        specific_risk=specific_risk_output,
        identity=identity,
    )


def build_barra_portfolio_weights(backtest: PortfolioBacktestResult) -> pl.LazyFrame:
    """把目标多头持仓转换为 Barra 归因要求的毛收益权重端口。"""

    weights = pl.DataFrame(backtest.weights)
    returns = pl.DataFrame(backtest.daily_returns)
    required_weights = {
        "signal_date", "entry_date", "exit_date", "security_id", "portfolio", "weight"
    }
    if set(weights.columns) != required_weights:
        raise _error("组合持仓字段必须精确符合目标多头合同")
    if weights.filter(pl.col("portfolio") != "target_long").limit(1).height:
        raise _error("组合持仓 portfolio 必须是 target_long")
    required_returns = {
        "entry_date",
        "exit_date",
        "target_long_gross_return",
        "benchmark_return",
    }
    missing_returns = required_returns.difference(returns.columns)
    if missing_returns:
        raise _error(f"组合回测缺少目标多头毛收益或基准收益：{sorted(missing_returns)}")
    realized = (
        returns.select(
            "entry_date",
            "exit_date",
            pl.col("target_long_gross_return").alias("realized_return"),
            "benchmark_return",
        )
    )
    return (
        weights.join(
            realized,
            on=["entry_date", "exit_date"],
            how="inner",
            validate="m:1",
        )
        .select(
            "signal_date",
            "entry_date",
            "portfolio",
            "security_id",
            "weight",
            "realized_return",
            "benchmark_return",
        )
        .sort(["entry_date", "portfolio", "security_id"])
        .lazy()
    )
