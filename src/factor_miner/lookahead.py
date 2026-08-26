"""在打开研究结果前执行确定性的动态未来依赖探针。"""

from __future__ import annotations

from datetime import date
import hashlib

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.compiler import CompiledFactorPlan, build_polars_expr
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.schema import FactorNode


class LookaheadProbeResult(BaseModel):
    """一次前缀截断与未来扰动检查的不可变结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoints: tuple[date, ...]
    seed: int
    compared_rows: int = Field(ge=0)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed: bool


def run_lookahead_probes(
    plan: CompiledFactorPlan,
    input_frame: pl.DataFrame,
    checkpoints: tuple[date, ...],
    seed: int,
) -> LookaheadProbeResult:
    """证明检查点及之前的因子值不依赖检查点之后的输入。"""

    _validate_input(plan, input_frame, checkpoints)
    ordered = input_frame.sort(["date", "asset"])
    full_output = _evaluate_plan(plan, ordered)
    compared_rows = 0
    for checkpoint in checkpoints:
        expected = full_output.filter(pl.col("date") <= checkpoint)
        prefix_input = ordered.filter(pl.col("date") <= checkpoint)
        prefix_output = _evaluate_plan(plan, prefix_input)
        if not _frames_equal(expected, prefix_output):
            raise FactorMinerError(
                FailureCode.LOOKAHEAD_DETECTED,
                f"前缀截断改变了 {checkpoint.isoformat()} 及之前的因子值",
            )
        perturbed = _perturb_future(ordered, plan.required_fields, checkpoint, seed)
        perturbed_output = _evaluate_plan(plan, perturbed).filter(
            pl.col("date") <= checkpoint
        )
        if not _frames_equal(expected, perturbed_output):
            raise FactorMinerError(
                FailureCode.LOOKAHEAD_DETECTED,
                f"未来输入扰动改变了 {checkpoint.isoformat()} 及之前的因子值",
            )
        compared_rows += expected.height * 2
    return LookaheadProbeResult(
        checkpoints=checkpoints,
        seed=seed,
        compared_rows=compared_rows,
        input_sha256=_input_hash(ordered),
        passed=True,
    )


def _validate_input(
    plan: CompiledFactorPlan,
    frame: pl.DataFrame,
    checkpoints: tuple[date, ...],
) -> None:
    """校验探针输入、字段和检查点。"""

    missing = {"date", "asset", *plan.required_fields} - set(frame.columns)
    if missing:
        raise FactorMinerError(
            FailureCode.FIELD_MISSING,
            f"未来依赖探针缺少输入列：{sorted(missing)}",
        )
    if not checkpoints or tuple(sorted(set(checkpoints))) != checkpoints:
        raise ValueError("checkpoints 必须非空、唯一且递增")
    minimum = frame.get_column("date").min()
    maximum = frame.get_column("date").max()
    if minimum is None or maximum is None:
        raise FactorMinerError(FailureCode.FACTOR_COVERAGE_TOO_LOW, "探针输入为空")
    if any(checkpoint < minimum or checkpoint >= maximum for checkpoint in checkpoints):
        raise ValueError("每个 checkpoint 必须位于输入日期内部并保留未来行")


def _evaluate_plan(plan: CompiledFactorPlan, frame: pl.DataFrame) -> pl.DataFrame:
    """在内存输入上执行实际白名单编译表达式。"""

    expression = FactorNode.model_validate(plan.expression)
    return (
        frame.lazy()
        .sort(["date", "asset"])
        .with_columns(build_polars_expr(expression).alias("raw_factor"))
        .select(["date", "asset", "raw_factor"])
        .sort(["date", "asset"])
        .collect()
    )


def _perturb_future(
    frame: pl.DataFrame,
    fields: tuple[str, ...],
    checkpoint: date,
    seed: int,
) -> pl.DataFrame:
    """只改变检查点之后的允许输入字段，空值位置保持不变。"""

    expressions: list[pl.Expr] = []
    for index, field in enumerate(fields):
        dtype = frame.schema[field]
        if not dtype.is_numeric():
            raise FactorMinerError(
                FailureCode.DSL_TYPE_ERROR,
                f"未来扰动仅支持数值输入字段：{field}={dtype}",
            )
        multiplier = -float(index + 2)
        offset = float((seed + 1) * (index + 1)) + 0.125
        expressions.append(
            pl.when(pl.col("date") > checkpoint)
            .then(pl.col(field).cast(pl.Float64) * multiplier + offset)
            .otherwise(pl.col(field).cast(pl.Float64))
            .alias(field)
        )
    return frame.with_columns(expressions)


def _frames_equal(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    """按稳定键和逐值空值相等语义比较因子输出。"""

    return left.sort(["date", "asset"]).equals(
        right.sort(["date", "asset"]), null_equal=True
    )


def _input_hash(frame: pl.DataFrame) -> str:
    """计算排序后输入面板的确定性内容哈希。"""

    payload = frame.sort(["date", "asset"]).write_json().encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
