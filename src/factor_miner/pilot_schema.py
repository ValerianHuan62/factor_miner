"""阶段 A 固定候选 Pilot 的不可变合同。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factor_miner.canonical import sha256_json
from factor_miner.portfolio_schema import TradingCalendarIdentity
from factor_miner.schema import CandidateFactorSpec, TrustedCandidateFactorSpec


SHA256_PATTERN = r"^[0-9a-f]{64}$"
COMMIT_PATTERN = r"^[0-9a-f]{40}$"


def _non_empty_path(value: Path | None, field_name: str) -> Path:
    """校验路径已显式提供且为绝对路径。"""

    if value is None:
        raise ValueError(f"{field_name} 必须显式提供")
    if not str(value).strip():
        raise ValueError(f"{field_name} 不能为空")
    path = value.expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field_name} 必须是绝对路径")
    return path


class PilotFixedCandidate(BaseModel):
    """一个人工冻结的阶段 A 候选。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.:-]+$")
    spec: CandidateFactorSpec | TrustedCandidateFactorSpec

    @property
    def spec_hash(self) -> str:
        """返回候选 Spec 的完整内容哈希。"""

        return sha256_json(self.spec.model_dump(mode="json"))


class PilotFixedCandidateFile(BaseModel):
    """阶段 A 固定候选文件的不可变内容。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["pilot-fixed-candidates-v1", "pilot-candidates-v2"]
    candidates: tuple[PilotFixedCandidate, ...]

    @model_validator(mode="after")
    def validate_fixed_set(self) -> PilotFixedCandidateFile:
        """确保候选集合恰好是三个预登记槽位。"""

        ids = tuple(candidate.candidate_id for candidate in self.candidates)
        expected = ("pilot_fixed_001", "pilot_fixed_002", "pilot_fixed_003")
        if self.version == "pilot-fixed-candidates-v1" and ids != expected:
            raise ValueError("固定候选必须按顺序恰好包含三个 pilot_fixed 槽位")
        if self.version == "pilot-candidates-v2" and (
            not ids or len(ids) != len(set(ids))
        ):
            raise ValueError("复算候选集合必须非空且 source_candidate_id 唯一")
        return self


class PilotSourcePaths(BaseModel):
    """服务器侧输入路径及显式沪深全市场身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    universe: Literal["SSE_SZSE_WHOLE_MARKET"] = "SSE_SZSE_WHOLE_MARKET"
    quantlake_root: Path
    release_manifest_uri: Path
    partition_manifest_root: Path | None = None
    partition_manifest_uri: Path | None = None
    state_manifest_uri: Path
    market_uri: Path
    state_uri: Path | None
    field_registry_uri: Path
    field_registry_root: Path | None = None
    benchmark_root: Path | None
    calendar_root: Path | None = None
    calendar_uri: Path | None
    calendar_manifest_uri: Path | None = None
    calendar_version: str
    benchmark_uri: Path | None
    benchmark_manifest_uri: Path | None = None
    benchmark_schema_version: str
    universe_root: Path | None = None
    universe_uri: Path | None = None
    market_open_root: Path | None = None
    market_open_uri: Path | None = None
    market_open_manifest_uri: Path | None = None
    barra_root: Path | None = None
    barra_manifest_uri: Path | None = None
    barra_max_exposure_staleness_days: int | None = Field(default=None, ge=0)
    barra_exposure_uri: Path | None = None
    barra_factor_returns_uri: Path | None = None
    barra_benchmark_weights_uri: Path | None = None
    barra_covariance_uri: Path | None = None
    barra_specific_risk_uri: Path | None = None

    @field_validator(
        "quantlake_root",
        "release_manifest_uri",
        "state_manifest_uri",
        "market_uri",
        "field_registry_uri",
        mode="before",
    )
    @classmethod
    def validate_required_path(cls, value: Path | str, info: object) -> Path:
        """拒绝隐式空路径。"""

        return _non_empty_path(Path(value), str(getattr(info, "field_name", "path")))

    @field_validator(
        "state_uri",
        "partition_manifest_uri",
        "calendar_uri",
        "calendar_manifest_uri",
        "benchmark_uri",
        "benchmark_manifest_uri",
        "universe_uri",
        "market_open_uri",
        "market_open_manifest_uri",
        "barra_manifest_uri",
        "barra_exposure_uri",
        "barra_factor_returns_uri",
        "barra_benchmark_weights_uri",
        "barra_covariance_uri",
        "barra_specific_risk_uri",
        mode="before",
    )
    @classmethod
    def validate_optional_path(cls, value: Path | str | None, info: object) -> Path | None:
        """保留缺失入口，交由对应合同给出稳定硬失败。"""

        if value is None or not str(value).strip():
            return None
        return _non_empty_path(Path(value), str(getattr(info, "field_name", "path")))

    @field_validator(
        "field_registry_root",
        "partition_manifest_root",
        "benchmark_root",
        "calendar_root",
        "universe_root",
        "market_open_root",
        "barra_root",
        mode="before",
    )
    @classmethod
    def validate_external_root(cls, value: Path | str | None, info: object) -> Path | None:
        """允许独立、显式配置的服务器派生输入根目录。"""

        if value is None or not str(value).strip():
            return None
        return _non_empty_path(Path(value), str(getattr(info, "field_name", "path")))

    @field_validator("calendar_version", "benchmark_schema_version")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """拒绝空版本标识。"""

        if not value.strip():
            raise ValueError("版本标识不能为空")
        return value

    @model_validator(mode="after")
    def validate_root_containment(self) -> PilotSourcePaths:
        """要求各输入位于其显式只读根内，Barra 可缺失但不能越界。"""

        root = self.quantlake_root.resolve(strict=False)
        paths = (
            self.release_manifest_uri,
            self.state_manifest_uri,
            self.market_uri,
            self.state_uri,
        )
        for path in paths:
            if path is None:
                continue
            resolved = path.resolve(strict=False)
            if resolved != root and root not in resolved.parents:
                raise ValueError(f"输入路径必须位于 QuantLake 根目录内：{path}")
        field_registry_root = (
            self.field_registry_root.resolve(strict=False)
            if self.field_registry_root is not None
            else None
        )
        field_registry = self.field_registry_uri.resolve(strict=False)
        allowed_field_roots = [root]
        if field_registry_root is not None:
            allowed_field_roots.append(field_registry_root)
        if not any(
            field_registry == item or item in field_registry.parents
            for item in allowed_field_roots
        ):
            raise ValueError(
                "field_registry_uri 必须位于 QuantLake 或显式 field_registry_root 内"
            )
        for path, external_root, name in (
            (
                self.partition_manifest_uri,
                self.partition_manifest_root,
                "partition_manifest_uri",
            ),
            (self.universe_uri, self.universe_root, "universe_uri"),
            (self.market_open_uri, self.market_open_root, "market_open_uri"),
            (
                self.market_open_manifest_uri,
                self.market_open_root,
                "market_open_manifest_uri",
            ),
        ):
            if path is None:
                continue
            if external_root is None:
                raise ValueError(f"{name} 必须同时显式提供对应 root")
            resolved_path = path.resolve(strict=False)
            resolved_root = external_root.resolve(strict=False)
            if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
                raise ValueError(f"{name} 必须位于显式派生 root 内")
        barra_paths = (
            (self.barra_manifest_uri, "barra_manifest_uri"),
            (self.barra_exposure_uri, "barra_exposure_uri"),
            (self.barra_factor_returns_uri, "barra_factor_returns_uri"),
            (self.barra_benchmark_weights_uri, "barra_benchmark_weights_uri"),
            (self.barra_covariance_uri, "barra_covariance_uri"),
            (self.barra_specific_risk_uri, "barra_specific_risk_uri"),
        )
        barra_data_paths = barra_paths[1:]
        if any(path is not None for path, _ in barra_data_paths):
            if self.barra_max_exposure_staleness_days is None:
                raise ValueError(
                    "启用 Barra URI 时必须显式提供 barra_max_exposure_staleness_days"
                )
            if self.barra_manifest_uri is None:
                raise ValueError("启用 Barra URI 时必须显式提供 barra_manifest_uri")
        for path, name in barra_paths:
            if path is None:
                continue
            resolved_path = path.resolve(strict=False)
            allowed_roots = [self.barra_root]
            if name == "barra_manifest_uri":
                allowed_roots.append(self.partition_manifest_root)
            resolved_roots = [
                item.resolve(strict=False) for item in allowed_roots if item is not None
            ]
            if not resolved_roots or not any(
                resolved_path == item or item in resolved_path.parents
                for item in resolved_roots
            ):
                raise ValueError(f"{name} 必须位于显式数据根或清单根内")
        benchmark_root = (
            self.benchmark_root.resolve(strict=False)
            if self.benchmark_root is not None
            else None
        )
        calendar_root = (
            self.calendar_root.resolve(strict=False)
            if self.calendar_root is not None
            else None
        )
        if self.benchmark_uri is not None:
            if benchmark_root is None:
                raise ValueError("benchmark_uri 必须同时显式提供 benchmark_root")
            benchmark = self.benchmark_uri.resolve(strict=False)
            if benchmark != benchmark_root and benchmark_root not in benchmark.parents:
                raise ValueError("benchmark_uri 必须位于显式 benchmark_root 内")
        if self.benchmark_manifest_uri is not None:
            benchmark_manifest = self.benchmark_manifest_uri.resolve(strict=False)
            manifest_roots = [
                item
                for item in (benchmark_root, self.partition_manifest_root)
                if item is not None
            ]
            if not any(
                benchmark_manifest == item.resolve(strict=False)
                or item.resolve(strict=False) in benchmark_manifest.parents
                for item in manifest_roots
            ):
                raise ValueError(
                    "benchmark_manifest_uri 必须位于基准根或显式清单根内"
                )
        if self.calendar_uri is not None:
            calendar = self.calendar_uri.resolve(strict=False)
            allowed = [root]
            if benchmark_root is not None:
                allowed.append(benchmark_root)
            if calendar_root is not None:
                allowed.append(calendar_root)
            if not any(calendar == item or item in calendar.parents for item in allowed):
                raise ValueError(
                    "calendar_uri 必须位于 QuantLake、benchmark_root 或显式 calendar_root 内"
                )
        if self.calendar_manifest_uri is not None:
            calendar_manifest = self.calendar_manifest_uri.resolve(strict=False)
            allowed = [root]
            if benchmark_root is not None:
                allowed.append(benchmark_root)
            if calendar_root is not None:
                allowed.append(calendar_root)
            if self.partition_manifest_root is not None:
                allowed.append(self.partition_manifest_root.resolve(strict=False))
            if not any(
                calendar_manifest == item or item in calendar_manifest.parents
                for item in allowed
            ):
                raise ValueError(
                    "calendar_manifest_uri 必须位于 QuantLake、benchmark_root 或显式 calendar_root 内"
                )
        return self


class BenchmarkIdentity(BaseModel):
    """沪深 300 基准输入的内容身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    benchmark: Literal["CSI300"] = "CSI300"
    source_uri: Path
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    schema_version: str = Field(min_length=1)
    cutoff: date
    price_column: Literal["open"] = "open"
    return_definition: Literal["open_to_open"] = "open_to_open"


class BarraAvailability(BaseModel):
    """Barra 可选输入的状态，不影响缺失时的 Pilot 发布。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["available", "not_available"]
    missing_inputs: tuple[str, ...] = ()
    source_uris: tuple[Path, ...] = ()
    input_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_status(self) -> BarraAvailability:
        """可用状态不得带缺失项，不可用状态必须留下原因。"""

        if self.status == "available" and self.missing_inputs:
            raise ValueError("Barra available 状态不能包含 missing_inputs")
        if self.status == "not_available" and not self.missing_inputs:
            raise ValueError("Barra not_available 状态必须记录缺失输入")
        return self


class PilotInputManifest(BaseModel):
    """一次 Pilot 所使用的服务器输入身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    data_origin: Literal["server_quantlake"] = "server_quantlake"
    universe: Literal["SSE_SZSE_WHOLE_MARKET"] = "SSE_SZSE_WHOLE_MARKET"
    quantlake_root: Path
    resolved_release_id: str = Field(min_length=1)
    release_manifest_uri: Path
    release_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    state_manifest_uri: Path
    state_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    schema_version: str = Field(min_length=1)
    market_cutoff: date
    state_table_version: str = Field(min_length=1)
    state_table_cutoff: date
    adjustment_convention: str = Field(min_length=1)
    market_uri: Path
    state_uri: Path
    field_registry_uri: Path
    field_registry_sha256: str = Field(pattern=SHA256_PATTERN)
    canonical_field_map: dict[str, str]
    universe_uri: Path | None = None
    universe_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    market_open_uri: Path | None = None
    market_open_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    calendar_uri: Path
    calendar: TradingCalendarIdentity
    benchmark: BenchmarkIdentity
    barra: BarraAvailability

    @model_validator(mode="after")
    def validate_cutoffs_and_fields(self) -> PilotInputManifest:
        """行情和状态截止日、候选必需字段映射必须一致。"""

        if self.market_cutoff != self.state_table_cutoff:
            raise ValueError("行情与状态表 cutoff 不一致")
        for field in ("close", "volume"):
            if field not in self.canonical_field_map:
                raise ValueError(f"字段注册表缺少候选必需字段：{field}")
        return self


class PilotRunRequest(BaseModel):
    """结果揭晓前冻结的 Pilot 请求。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    visible_start: date
    visible_end: date
    candidate_file_sha256: str = Field(pattern=SHA256_PATTERN)
    evaluation_policy_id: str = Field(min_length=1)
    data_release_id: str = Field(min_length=1)
    code_commit: str = Field(pattern=COMMIT_PATTERN)
    config_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_root: Path

    @field_validator("artifact_root", mode="before")
    @classmethod
    def validate_artifact_root(cls, value: Path | str) -> Path:
        """产物根目录必须显式为绝对路径。"""

        return _non_empty_path(Path(value), "artifact_root")

    @model_validator(mode="after")
    def validate_dates(self) -> PilotRunRequest:
        """拒绝空的可见区间。"""

        if self.visible_end < self.visible_start:
            raise ValueError("Pilot visible_end 不能早于 visible_start")
        return self


class PilotPublicationStatus(BaseModel):
    """Pilot 发布判定；候选指标好坏不参与该判定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["published", "blocked"]
    reason: str = Field(min_length=1)
