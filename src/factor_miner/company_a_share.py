"""公司 A 股显式 Parquet URI 数据适配器。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import os

import polars as pl

from factor_miner.data_source import (
    IDENTITY_COLUMNS,
    LABEL_COLUMNS,
    MARKET_COLUMNS,
    MASK_COLUMNS,
    STATE_BASE_COLUMNS,
    DataProvenance,
    DataRequest,
    FactorInputRequest,
    InputProvenance,
    OutcomeProvenance,
    OutcomeRequest,
    ReferenceProvenance,
)
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.runtime import RuntimeProfile
from factor_miner.schema import EvaluationPolicySpec


class CompanyAShareDataSource:
    """读取公司 A 股三张显式 Parquet 表并执行数据合同校验。"""

    def __init__(self, profile: RuntimeProfile, code_commit: str, config_hash: str) -> None:
        """保存运行配置，不在构造阶段访问任何数据文件。"""

        self._profile = profile
        self._code_commit = code_commit
        self._config_hash = config_hash
        self._provenance: DataProvenance | None = None

    @staticmethod
    def request(
        start: date, end: date, required_fields: tuple[str, ...], warmup_days: int
    ) -> DataRequest:
        """提供便于调用方构造请求的显式工厂。"""

        return DataRequest(start, end, required_fields, warmup_days)

    @property
    def quantlake_root(self) -> Path | None:
        """返回真实上游根目录，供 artifact 写入边界检查使用。"""

        return self._profile.quantlake_root

    def inspect(self) -> DataProvenance:
        """完整检查三张 Parquet 表并缓存通过的 provenance。"""

        provenance = self._build_provenance()
        self._validate_paths()
        market = self._read_table(self._required_uri("market_uri"), "行情")
        state = self._read_table(self._required_uri("state_uri"), "状态")
        label = self._read_table(self._required_uri("label_uri"), "标签")
        self._validate_schema(market, state, label)
        self._validate_keys(market, "行情")
        self._validate_keys(state, "状态")
        self._validate_keys(label, "标签")
        market_cutoff = self._cutoff(market, "行情")
        state_cutoff = self._cutoff(state, "状态")
        label_cutoff = self._cutoff(label, "标签")
        expected_market_cutoff = self._parse_cutoff(provenance.market_cutoff, "行情")
        expected_state_cutoff = self._parse_cutoff(provenance.state_table_cutoff, "状态")
        if market_cutoff != state_cutoff or market_cutoff != label_cutoff:
            raise self._error(
                FailureCode.DATA_RELEASE_MISMATCH,
                f"行情、状态和标签 cutoff 不一致：{market_cutoff}, {state_cutoff}, {label_cutoff}",
            )
        if market_cutoff != expected_market_cutoff or state_cutoff != expected_state_cutoff:
            raise self._error(
                FailureCode.DATA_RELEASE_MISMATCH,
                "文件 cutoff 与 provenance 声明不一致",
            )
        self._validate_masks(state)
        self._validate_join_keys(market, state, label)
        self._provenance = provenance
        return provenance

    def validate_contract(self) -> None:
        """执行完整合同检查，失败时保留未验证状态。"""

        self.inspect()

    def scan(self, request: DataRequest) -> pl.LazyFrame:
        """按请求投影和连接数据，并保留指定 warmup 区间。"""

        if self._provenance is None:
            raise self._error(
                FailureCode.STATE_COVERAGE_INCOMPLETE,
                "必须先调用 inspect 并通过数据合同校验，才允许 scan",
            )
        unknown_fields = set(request.required_fields) - set(MARKET_COLUMNS)
        if unknown_fields:
            raise self._error(
                FailureCode.FIELD_MISSING,
                f"请求字段不在行情标准列中：{sorted(unknown_fields)}",
            )

        lower_bound = request.start - timedelta(days=request.warmup_days)
        market = (
            pl.scan_parquet(self._required_uri("market_uri"))
            .filter(pl.col("date").is_between(lower_bound, request.end, closed="both"))
            .select([*IDENTITY_COLUMNS, *request.required_fields])
        )
        state = (
            pl.scan_parquet(self._required_uri("state_uri"))
            .filter(pl.col("date").is_between(lower_bound, request.end, closed="both"))
            .select([*IDENTITY_COLUMNS, *MASK_COLUMNS])
        )
        label = (
            pl.scan_parquet(self._required_uri("label_uri"))
            .filter(pl.col("date").is_between(lower_bound, request.end, closed="both"))
            .select([*IDENTITY_COLUMNS, *LABEL_COLUMNS])
        )
        return (
            market.join(state, on=list(IDENTITY_COLUMNS), how="inner", validate="1:1")
            .join(label, on=list(IDENTITY_COLUMNS), how="left", validate="1:1")
            .sort(list(IDENTITY_COLUMNS))
        )

    def _build_provenance(self) -> DataProvenance:
        """从运行配置构造并验证完整 provenance。"""

        try:
            return DataProvenance(
                data_origin=self._profile.data_origin or "",
                resolved_release_id=self._profile.resolved_release_id or "",
                release_manifest_sha256=self._profile.release_manifest_sha256 or "",
                schema_version=self._profile.schema_version or "",
                market_cutoff=self._profile.market_cutoff or "",
                adjustment_convention=self._profile.adjustment_convention or "",
                calendar_version=self._profile.calendar_version or "",
                state_table_version=self._profile.state_table_version or "",
                state_table_cutoff=self._profile.state_table_cutoff or "",
                code_commit=self._code_commit,
                config_hash=self._config_hash,
            )
        except ValueError as error:
            raise self._error(
                FailureCode.DATA_RELEASE_MISMATCH,
                f"数据 provenance 不完整：{error}",
            ) from error

    def _validate_paths(self) -> None:
        """验证显式 URI 存在，并检查真实模式 QuantLake 只读。"""

        for name in ("market_uri", "state_uri", "label_uri"):
            path = self._required_uri(name)
            if not path.is_file():
                raise self._error(FailureCode.FIELD_MISSING, f"{name} 不是可读文件：{path}")
        quantlake_root = self._profile.quantlake_root
        if self._profile.mode.value in {"smoke", "visible"}:
            if quantlake_root is None or not quantlake_root.is_dir():
                raise self._error(
                    FailureCode.RUNTIME_BOUNDARY_ERROR,
                    "真实模式必须配置存在的 QuantLake 根目录",
                )
            if os.access(quantlake_root, os.W_OK):
                raise self._error(
                    FailureCode.RUNTIME_BOUNDARY_ERROR,
                    "当前运行用户对 QuantLake 根目录具备写权限",
                )

    def _validate_schema(self, market: pl.DataFrame, state: pl.DataFrame, label: pl.DataFrame) -> None:
        """验证三张表的标准列和基础类型。"""

        required = {
            "行情": set(IDENTITY_COLUMNS + MARKET_COLUMNS),
            "状态": set(IDENTITY_COLUMNS + STATE_BASE_COLUMNS + MASK_COLUMNS),
            "标签": set(IDENTITY_COLUMNS + LABEL_COLUMNS),
        }
        tables = {"行情": market, "状态": state, "标签": label}
        for name, table in tables.items():
            missing = required[name] - set(table.columns)
            if missing:
                raise self._error(
                    FailureCode.FIELD_MISSING,
                    f"{name}表缺少标准列：{sorted(missing)}",
                )
        if market.schema.get("date") != pl.Date or state.schema.get("date") != pl.Date:
            raise self._error(FailureCode.FIELD_MISSING, "行情和状态 date 必须是 Date 类型")
        if label.schema.get("date") != pl.Date:
            raise self._error(FailureCode.FIELD_MISSING, "标签 date 必须是 Date 类型")
        for name, table in (("状态", state),):
            for column in STATE_BASE_COLUMNS + MASK_COLUMNS:
                if table.schema.get(column) != pl.Boolean:
                    raise self._error(
                        FailureCode.STATE_COVERAGE_INCOMPLETE,
                        f"{name}列 {column} 必须是 Boolean 类型",
                    )

    def _validate_keys(self, table: pl.DataFrame, name: str) -> None:
        """验证 date/asset 主键没有重复或空值。"""

        if table.select(pl.col("date").is_null().any()).item() or table.select(
            pl.col("asset").is_null().any()
        ).item():
            raise self._error(FailureCode.DATA_RELEASE_MISMATCH, f"{name}主键不能包含空值")
        duplicate = table.group_by(list(IDENTITY_COLUMNS)).len().filter(pl.col("len") > 1)
        if duplicate.height:
            raise self._error(
                FailureCode.DATA_RELEASE_MISMATCH,
                f"{name}存在重复 date/asset 主键：{duplicate.head(1).to_dicts()}",
            )

    def _validate_join_keys(
        self, market: pl.DataFrame, state: pl.DataFrame, label: pl.DataFrame
    ) -> None:
        """验证行情、状态和标签的键集合完全一致。"""

        market_keys = market.select(list(IDENTITY_COLUMNS)).unique()
        for name, table in (("状态", state), ("标签", label)):
            missing = market_keys.join(
                table.select(list(IDENTITY_COLUMNS)).unique(),
                on=list(IDENTITY_COLUMNS),
                how="anti",
            )
            extra = table.select(list(IDENTITY_COLUMNS)).unique().join(
                market_keys, on=list(IDENTITY_COLUMNS), how="anti"
            )
            if missing.height or extra.height:
                raise self._error(
                    FailureCode.STATE_COVERAGE_INCOMPLETE,
                    f"{name}键集合与行情不一致：缺失 {missing.height}，多余 {extra.height}",
                )

    def _validate_masks(self, state: pl.DataFrame) -> None:
        """按基础状态重新计算并核对三个派生 mask。"""

        expected = state.with_columns(
            (~(pl.col("is_st") | pl.col("is_newly_listed"))).alias("expected_compute")
        ).with_columns(
            (pl.col("expected_compute") & ~pl.col("is_suspended")).alias("expected_rank"),
            (
                pl.col("expected_compute")
                & pl.col("can_buy")
                & pl.col("can_sell")
            ).alias("expected_trading"),
        )
        mismatches = expected.filter(
            (pl.col("valid_for_factor_compute") != pl.col("expected_compute"))
            | (pl.col("valid_for_factor_rank") != pl.col("expected_rank"))
            | (pl.col("valid_for_trading") != pl.col("expected_trading"))
        )
        if mismatches.height:
            raise self._error(
                FailureCode.STATE_COVERAGE_INCOMPLETE,
                f"派生状态 mask 与合同语义不一致：{mismatches.head(1).to_dicts()}",
            )

    @staticmethod
    def _read_table(path: Path, name: str) -> pl.DataFrame:
        """读取单个 Parquet 表并将底层读取错误转为领域错误。"""

        try:
            return pl.read_parquet(path)
        except Exception as error:
            raise FactorMinerError(
                FailureCode.FIELD_MISSING, f"无法读取{name}表 {path}：{error}"
            ) from error

    @staticmethod
    def _cutoff(table: pl.DataFrame, name: str) -> date:
        """返回表内最大日期。"""

        value = table.select(pl.col("date").max()).item()
        if value is None:
            raise FactorMinerError(FailureCode.DATA_RELEASE_MISMATCH, f"{name}表为空")
        return value

    @staticmethod
    def _parse_cutoff(value: str, name: str) -> date:
        """解析 provenance 中的 ISO cutoff 日期。"""

        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise FactorMinerError(
                FailureCode.DATA_RELEASE_MISMATCH,
                f"{name} cutoff 不是 ISO 日期：{value}",
            ) from error

    def _required_uri(self, attribute: str) -> Path:
        """读取 profile 中的必需 URI。"""

        path = getattr(self._profile, attribute)
        if path is None:
            raise self._error(FailureCode.FIELD_MISSING, f"缺少显式 URI：{attribute}")
        return path

    @staticmethod
    def _error(code: FailureCode, message: str) -> FactorMinerError:
        """构造带稳定错误代码的合同异常。"""

        return FactorMinerError(code, message)


class CompanyAShareFactorInputSource:
    """只读取 market/state 的 V0.1 公司 A 股 input 适配器。"""

    def __init__(self, profile: RuntimeProfile, code_commit: str, config_hash: str) -> None:
        """保存配置；构造阶段不访问任何文件。"""

        self._profile = profile
        self._legacy_validator = CompanyAShareDataSource(
            profile,
            code_commit=code_commit,
            config_hash=config_hash,
        )
        self._provenance: InputProvenance | None = None

    @property
    def quantlake_root(self) -> Path | None:
        """暴露只读 QuantLake 根目录供 artifact 边界校验。"""

        return self._profile.quantlake_root

    def inspect_inputs(self) -> InputProvenance:
        """仅检查行情和状态合同，绝不访问 label URI。"""

        market_path = self._required_existing_uri("market_uri")
        state_path = self._required_existing_uri("state_uri")
        self._validate_quantlake_read_only()
        market = CompanyAShareDataSource._read_table(market_path, "行情")
        state = CompanyAShareDataSource._read_table(state_path, "状态")
        self._validate_input_schema(market, state)
        self._legacy_validator._validate_keys(market, "行情")
        self._legacy_validator._validate_keys(state, "状态")
        market_cutoff = CompanyAShareDataSource._cutoff(market, "行情")
        state_cutoff = CompanyAShareDataSource._cutoff(state, "状态")
        expected_market_cutoff = CompanyAShareDataSource._parse_cutoff(
            self._profile.market_cutoff or "", "行情"
        )
        expected_state_cutoff = CompanyAShareDataSource._parse_cutoff(
            self._profile.state_table_cutoff or "", "状态"
        )
        if (
            market_cutoff != state_cutoff
            or market_cutoff != expected_market_cutoff
            or state_cutoff != expected_state_cutoff
        ):
            raise self._error(
                FailureCode.DATA_RELEASE_MISMATCH,
                "行情/状态 cutoff 与 provenance 不一致",
            )
        self._legacy_validator._validate_masks(state)
        _validate_exact_key_set(market, state, "状态")
        legacy = self._legacy_validator._build_provenance()
        provenance = InputProvenance(
            data_origin=legacy.data_origin,
            resolved_release_id=legacy.resolved_release_id,
            release_manifest_sha256=legacy.release_manifest_sha256,
            schema_version=legacy.schema_version,
            market_cutoff=legacy.market_cutoff,
            adjustment_convention=legacy.adjustment_convention,
            calendar_version=legacy.calendar_version,
            state_table_version=legacy.state_table_version,
            state_table_cutoff=legacy.state_table_cutoff,
            code_commit=legacy.code_commit,
            config_hash=legacy.config_hash,
        )
        self._provenance = provenance
        return provenance

    def scan_inputs(self, request: FactorInputRequest) -> pl.LazyFrame:
        """按真实交易观察数读取并校验每个资产的预热历史。"""

        if self._provenance is None:
            raise self._error(
                FailureCode.STATE_COVERAGE_INCOMPLETE,
                "必须先通过 inspect_inputs",
            )
        unknown_fields = set(request.required_fields) - set(MARKET_COLUMNS)
        if unknown_fields:
            raise self._error(
                FailureCode.FIELD_MISSING,
                f"请求字段不在行情标准列中：{sorted(unknown_fields)}",
            )
        market_path = self._required_uri("market_uri")
        trading_dates = (
            pl.scan_parquet(market_path)
            .select("date")
            .filter(pl.col("date") < request.start)
            .unique()
            .sort("date")
            .collect()
            .get_column("date")
            .to_list()
        )
        if len(trading_dates) < request.warmup_observations:
            raise self._error(
                FailureCode.FACTOR_COVERAGE_TOO_LOW,
                f"交易日历历史不足：需要 {request.warmup_observations}，实际 {len(trading_dates)}",
            )
        lower_bound = (
            trading_dates[-request.warmup_observations]
            if request.warmup_observations
            else request.start
        )
        market = (
            pl.scan_parquet(market_path)
            .filter(pl.col("date").is_between(lower_bound, request.end, closed="both"))
            .select([*IDENTITY_COLUMNS, *request.required_fields])
        )
        state = (
            pl.scan_parquet(self._required_uri("state_uri"))
            .filter(pl.col("date").is_between(lower_bound, request.end, closed="both"))
            .select([*IDENTITY_COLUMNS, *MASK_COLUMNS])
        )
        frame = market.join(
            state,
            on=list(IDENTITY_COLUMNS),
            how="inner",
            validate="1:1",
        ).sort(list(IDENTITY_COLUMNS)).collect()
        insufficient = (
            frame.with_columns(
                (
                    pl.col("date").cum_count().over("asset") - 1
                ).alias("history_count")
            )
            .filter(
                (pl.col("date") >= request.start)
                & (pl.col("valid_for_factor_compute") == True)
                & (pl.col("history_count") < request.warmup_observations)
            )
            .select(["date", "asset", "history_count"])
        )
        if insufficient.height:
            raise self._error(
                FailureCode.FACTOR_COVERAGE_TOO_LOW,
                "资产交易观察历史不足："
                f"{insufficient.sort('asset').head(5).to_dicts()}",
            )
        return frame.lazy()

    def _validate_input_schema(self, market: pl.DataFrame, state: pl.DataFrame) -> None:
        """验证 market/state 标准列、日期和 Boolean mask 类型。"""

        missing_market = set(IDENTITY_COLUMNS + MARKET_COLUMNS) - set(market.columns)
        missing_state = set(IDENTITY_COLUMNS + STATE_BASE_COLUMNS + MASK_COLUMNS) - set(
            state.columns
        )
        if missing_market or missing_state:
            raise self._error(
                FailureCode.FIELD_MISSING,
                f"input 缺少标准列：行情 {sorted(missing_market)}，状态 {sorted(missing_state)}",
            )
        if market.schema.get("date") != pl.Date or state.schema.get("date") != pl.Date:
            raise self._error(FailureCode.FIELD_MISSING, "行情和状态 date 必须是 Date 类型")
        for column in STATE_BASE_COLUMNS + MASK_COLUMNS:
            if state.schema.get(column) != pl.Boolean:
                raise self._error(
                    FailureCode.STATE_COVERAGE_INCOMPLETE,
                    f"状态列 {column} 必须是 Boolean 类型",
                )

    def _validate_quantlake_read_only(self) -> None:
        """真实 input 检查保留 QuantLake 只读边界。"""

        if self._profile.mode.value != "visible":
            return
        root = self._profile.quantlake_root
        if root is None or not root.is_dir():
            raise self._error(
                FailureCode.RUNTIME_BOUNDARY_ERROR,
                "真实模式必须配置存在的 QuantLake 根目录",
            )
        if os.access(root, os.W_OK):
            raise self._error(
                FailureCode.RUNTIME_BOUNDARY_ERROR,
                "当前运行用户对 QuantLake 根目录具备写权限",
            )

    def _required_existing_uri(self, attribute: str) -> Path:
        """读取并验证一个 input URI 存在。"""

        path = self._required_uri(attribute)
        if not path.is_file():
            raise self._error(FailureCode.FIELD_MISSING, f"{attribute} 不是可读文件：{path}")
        return path

    def _required_uri(self, attribute: str) -> Path:
        """读取 profile 中的 input URI。"""

        path = getattr(self._profile, attribute)
        if path is None:
            raise self._error(FailureCode.FIELD_MISSING, f"缺少显式 URI：{attribute}")
        return path

    @staticmethod
    def _error(code: FailureCode, message: str) -> FactorMinerError:
        """构造 input 合同错误。"""

        return FactorMinerError(code, message)


class CompanyAShareOutcomeSource:
    """只读取冻结 label release 的 V0.1 outcome 适配器。"""

    def __init__(self, profile: RuntimeProfile, policy: EvaluationPolicySpec) -> None:
        """保存 profile/policy；构造阶段不访问 label。"""

        self._profile = profile
        self._policy = policy
        self._provenance: OutcomeProvenance | None = None

    def inspect_outcomes(self) -> OutcomeProvenance:
        """只检查 label 文件、schema、主键、cutoff 和 policy 绑定。"""

        path = self._required_label_uri()
        if not path.is_file():
            raise self._error(FailureCode.FIELD_MISSING, f"label_uri 不是可读文件：{path}")
        label = CompanyAShareDataSource._read_table(path, "标签")
        required = {*IDENTITY_COLUMNS, self._policy.label_column}
        missing = required - set(label.columns)
        if missing:
            raise self._error(
                FailureCode.FIELD_MISSING,
                f"标签表缺少冻结列：{sorted(missing)}",
            )
        if label.schema.get("date") != pl.Date:
            raise self._error(FailureCode.FIELD_MISSING, "标签 date 必须是 Date 类型")
        CompanyAShareDataSource._validate_keys(self, label, "标签")
        cutoff = CompanyAShareDataSource._cutoff(label, "标签")
        expected_cutoff = CompanyAShareDataSource._parse_cutoff(
            self._profile.market_cutoff or "", "标签"
        )
        if cutoff != expected_cutoff:
            raise self._error(
                FailureCode.DATA_RELEASE_MISMATCH,
                "标签 cutoff 与冻结 input release 不一致",
            )
        try:
            provenance = OutcomeProvenance(
                label_id=self._policy.label_id,
                label_manifest_sha256=self._profile.release_manifest_sha256 or "",
                label_schema_version=self._profile.schema_version or "",
                label_cutoff=cutoff.isoformat(),
                label_formula_version=self._policy.label_formula_version,
                source_release_id=self._profile.resolved_release_id or "",
            )
        except ValueError as error:
            raise self._error(
                FailureCode.DATA_RELEASE_MISMATCH,
                f"outcome provenance 不完整：{error}",
            ) from error
        self._provenance = provenance
        return provenance

    def inspect_outcome_metadata(self) -> None:
        """只读取 Parquet 元数据校验冻结标签列，不读取任何标签值。"""

        path = self._required_label_uri()
        if not path.is_file():
            raise self._error(FailureCode.FIELD_MISSING, f"label_uri 不是可读文件：{path}")
        try:
            schema = pl.scan_parquet(path).collect_schema()
        except Exception as error:
            raise self._error(FailureCode.FIELD_MISSING, f"无法读取标签元数据：{error}") from error
        missing = {*IDENTITY_COLUMNS, self._policy.label_column} - set(schema.names())
        if missing or schema.get("date") != pl.Date:
            raise self._error(
                FailureCode.FIELD_MISSING,
                f"标签元数据不符合冻结政策：缺少 {sorted(missing)}",
            )

    def scan_outcomes(self, request: OutcomeRequest) -> pl.LazyFrame:
        """只返回 visible 区间的 date/asset/冻结 label。"""

        if self._provenance is None:
            raise self._error(
                FailureCode.DATA_RELEASE_MISMATCH,
                "必须先通过 inspect_outcomes",
            )
        return (
            pl.scan_parquet(self._required_label_uri())
            .filter(pl.col("date").is_between(request.start, request.end, closed="both"))
            .select([*IDENTITY_COLUMNS, self._policy.label_column])
            .sort(list(IDENTITY_COLUMNS))
        )

    def _required_label_uri(self) -> Path:
        """读取显式 label URI，不访问 market/state URI。"""

        path = self._profile.label_uri
        if path is None:
            raise self._error(FailureCode.FIELD_MISSING, "缺少显式 URI：label_uri")
        return path

    @staticmethod
    def _error(code: FailureCode, message: str) -> FactorMinerError:
        """构造 outcome 合同错误。"""

        return FactorMinerError(code, message)


class ParquetReferenceFactorSource:
    """读取显式冻结 Parquet reference factor 集合。"""

    def __init__(
        self,
        *,
        manifest_id: str,
        manifest_sha256: str,
        factor_paths: dict[str, Path],
        data_cutoff: str,
    ) -> None:
        """保存 reference manifest；构造阶段不访问文件。"""

        self._manifest_id = manifest_id
        self._manifest_sha256 = manifest_sha256
        self._factor_paths = dict(factor_paths)
        self._data_cutoff = data_cutoff
        self._provenance: ReferenceProvenance | None = None

    def inspect_references(self, manifest_id: str) -> ReferenceProvenance:
        """验证 manifest ID、文件 schema、主键和 cutoff。"""

        if manifest_id != self._manifest_id:
            raise FactorMinerError(
                FailureCode.DATA_RELEASE_MISMATCH,
                "请求的 reference manifest 与冻结配置不一致",
            )
        if not self._factor_paths:
            raise FactorMinerError(FailureCode.FIELD_MISSING, "reference factor set 为空")
        expected_cutoff = CompanyAShareDataSource._parse_cutoff(
            self._data_cutoff, "reference"
        )
        for factor_id, path in self._factor_paths.items():
            if not factor_id.strip() or not path.is_file():
                raise FactorMinerError(
                    FailureCode.FIELD_MISSING,
                    f"reference 文件缺失：{factor_id} -> {path}",
                )
            frame = CompanyAShareDataSource._read_table(path, factor_id)
            missing = {"date", "asset", "raw_factor"} - set(frame.columns)
            if missing or frame.schema.get("date") != pl.Date:
                raise FactorMinerError(
                    FailureCode.FIELD_MISSING,
                    f"reference {factor_id} schema 非法：{sorted(missing)}",
                )
            CompanyAShareDataSource._validate_keys(self, frame, factor_id)
            if CompanyAShareDataSource._cutoff(frame, factor_id) != expected_cutoff:
                raise FactorMinerError(
                    FailureCode.DATA_RELEASE_MISMATCH,
                    f"reference {factor_id} cutoff 不一致",
                )
        provenance = ReferenceProvenance(
            manifest_id=self._manifest_id,
            manifest_sha256=self._manifest_sha256,
            factor_ids=tuple(self._factor_paths),
            data_cutoff=self._data_cutoff,
        )
        self._provenance = provenance
        return provenance

    def scan_reference(
        self,
        factor_id: str,
        start: date,
        end: date,
    ) -> pl.LazyFrame:
        """返回一个已验证 reference 的 visible 区间。"""

        if self._provenance is None:
            raise FactorMinerError(
                FailureCode.DATA_RELEASE_MISMATCH,
                "必须先通过 inspect_references",
            )
        path = self._factor_paths.get(factor_id)
        if path is None:
            raise FactorMinerError(
                FailureCode.FIELD_MISSING,
                f"reference factor 未在冻结 manifest 中：{factor_id}",
            )
        if end < start:
            raise ValueError("reference end 不能早于 start")
        return (
            pl.scan_parquet(path)
            .filter(pl.col("date").is_between(start, end, closed="both"))
            .select(["date", "asset", "raw_factor"])
            .sort(["date", "asset"])
        )

    @staticmethod
    def _error(code: FailureCode, message: str) -> FactorMinerError:
        """构造 reference 合同错误，供共享主键校验使用。"""

        return FactorMinerError(code, message)


def _validate_exact_key_set(
    left: pl.DataFrame,
    right: pl.DataFrame,
    right_name: str,
) -> None:
    """验证两张表的 date/asset 键集合完全一致。"""

    left_keys = left.select(list(IDENTITY_COLUMNS)).unique()
    right_keys = right.select(list(IDENTITY_COLUMNS)).unique()
    missing = left_keys.join(right_keys, on=list(IDENTITY_COLUMNS), how="anti")
    extra = right_keys.join(left_keys, on=list(IDENTITY_COLUMNS), how="anti")
    if missing.height or extra.height:
        raise FactorMinerError(
            FailureCode.STATE_COVERAGE_INCOMPLETE,
            f"{right_name}键集合与行情不一致：缺失 {missing.height}，多余 {extra.height}",
        )
