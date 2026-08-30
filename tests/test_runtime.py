import tempfile
import unittest
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.runtime import (
    ExecutionMode,
    RuntimeProfile,
    _absolute_path_without_symlink_resolution,
    assert_real_data_allowed,
    canonical_config_hash,
    load_runtime_profile,
    verify_runtime_identity,
)


def complete_env(mode: str = "visible") -> dict[str, str]:
    """构造一份完整的运行时测试配置。

    参数：
        mode: 运行模式名称。

    返回：
        可直接传给运行时加载器的环境变量字典。
    """

    environment = {
        "FM_MODE": mode,
        "FM_QUANTLAKE_ROOT": "/data/quantlake",
        "FM_ARTIFACT_ROOT": "/data/factor_miner_artifacts",
        "FM_MARKET_URI": "/data/quantlake/releases/r1/market.parquet",
        "FM_STATE_URI": "/data/quantlake/releases/r1/state.parquet",
        "FM_LABEL_URI": "/data/quantlake/releases/r1/label.parquet",
        "FM_DATA_ORIGIN": "server_quantlake",
        "FM_RESOLVED_RELEASE_ID": "release-r1",
        "FM_RELEASE_MANIFEST_PATH": "/data/quantlake/releases/r1/manifest.json",
        "FM_RELEASE_MANIFEST_SHA256": "a" * 64,
        "FM_CONFIG_PATH": "/data/factor_miner_artifacts/config/private.env",
        "FM_CONFIG_HASH": "b" * 64,
        "FM_CODE_COMMIT": "c" * 40,
        "FM_SCHEMA_VERSION": "schema-v1",
        "FM_MARKET_CUTOFF": "2026-07-15",
        "FM_ADJUSTMENT_CONVENTION": "前复权",
        "FM_CALENDAR_VERSION": "calendar-v1",
        "FM_STATE_TABLE_VERSION": "state-v1",
        "FM_STATE_TABLE_CUTOFF": "2026-07-15",
    }
    if mode == "smoke":
        environment["FM_MARKET_URI"] = "/data/factor_miner_artifacts/inputs/market.parquet"
        environment["FM_STATE_URI"] = "/data/factor_miner_artifacts/inputs/state.parquet"
        environment["FM_LABEL_URI"] = ""
    return environment


class RuntimeBoundaryTest(unittest.TestCase):
    """运行时平台、路径和 provenance 边界测试。"""

    def test_visible_profile_passes_real_data_assertion(self) -> None:
        """验证完整 Linux visible 配置允许真实数据准入。"""
        profile = load_runtime_profile(complete_env(), platform_name="Linux")
        self.assertIsNone(assert_real_data_allowed(profile))

    def test_invalid_mode_is_rejected(self) -> None:
        """验证未知运行模式被拒绝。"""
        with self.assertRaises(FactorMinerError):
            load_runtime_profile(complete_env(mode="unknown"), platform_name="Linux")

    def test_visible_mode_accepts_darwin(self) -> None:
        """验证 visible 模式不再绑定 Linux。"""
        env = complete_env(mode="visible")
        profile = load_runtime_profile(env, platform_name="Darwin")
        self.assertEqual(profile.platform_name, "Darwin")

    def test_quantlake_is_an_optional_input_root(self) -> None:
        """验证真实模式可使用任意显式输入发布根目录。"""
        env = complete_env(mode="visible")
        env["FM_QUANTLAKE_ROOT"] = "/Users/example/data"
        profile = load_runtime_profile(env, platform_name="Darwin")
        self.assertEqual(profile.quantlake_root, Path("/Users/example/data"))

    def test_quantlake_config_path_can_be_a_server_symlink(self) -> None:
        """服务器 QuantLake 软链接必须保留配置路径用于精确边界判断。"""
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            target = root / "quantlake_release"
            target.mkdir()
            link = root / "quantlake"
            link.symlink_to(target, target_is_directory=True)
            configured = _absolute_path_without_symlink_resolution(
                str(link), "FM_QUANTLAKE_ROOT"
            )
            self.assertEqual(configured, link.absolute())
            self.assertNotEqual(configured, target.resolve())

    def test_artifacts_cannot_live_inside_quantlake(self) -> None:
        """验证 artifact root 不能位于 QuantLake 内。"""
        env = complete_env(mode="visible")
        env["FM_ARTIFACT_ROOT"] = "/data/quantlake/results"
        with self.assertRaises(FactorMinerError):
            load_runtime_profile(env, platform_name="Linux")

    def test_missing_provenance_is_rejected(self) -> None:
        """验证缺失 provenance 字段时硬失败。"""
        env = complete_env(mode="visible")
        env["FM_RELEASE_MANIFEST_SHA256"] = ""
        with self.assertRaises(FactorMinerError):
            load_runtime_profile(env, platform_name="Linux")

    def test_manifest_hash_must_be_lowercase_sha256(self) -> None:
        """正式配置不能用非十六进制占位符冒充清单哈希。"""

        env = complete_env(mode="visible")
        env["FM_RELEASE_MANIFEST_SHA256"] = "manifest-sha256"
        with self.assertRaisesRegex(FactorMinerError, "RUNTIME_BOUNDARY_ERROR"):
            load_runtime_profile(env, platform_name="Linux")

    def test_visible_rejects_smoke_input_root(self) -> None:
        """正式可见验证不能读取冒烟测试的有界输入。"""

        env = complete_env(mode="visible")
        env["FM_MARKET_URI"] = "/data/factor_miner_artifacts/inputs/market.parquet"
        with self.assertRaisesRegex(FactorMinerError, "RUNTIME_BOUNDARY_ERROR"):
            load_runtime_profile(env, platform_name="Linux")

    def test_visible_accepts_explicit_quantlake_derived_release_root(self) -> None:
        """可见模式允许显式声明且独立核验的 QuantLake 服务器侧派生发布。"""

        env = complete_env(mode="visible")
        derived_root = "/data/factor_miner_derived/releases/r1"
        env["FM_DERIVED_RELEASE_ROOT"] = derived_root
        env["FM_MARKET_URI"] = f"{derived_root}/market.parquet"
        env["FM_STATE_URI"] = f"{derived_root}/state.parquet"
        env["FM_LABEL_URI"] = f"{derived_root}/label.parquet"
        profile = load_runtime_profile(env, platform_name="Linux")
        self.assertEqual(profile.derived_release_root, Path(derived_root))

    def test_content_identity_checks_manifest_config_git_and_release_root(self) -> None:
        """实际文件、发布根目录、配置和 Git 身份必须相互一致。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quantlake = root / "quantlake"
            release_root = quantlake / "releases" / "r1"
            label_root = quantlake / "labels" / "l1"
            release_root.mkdir(parents=True)
            label_root.mkdir(parents=True)
            market = release_root / "market.parquet"
            state = release_root / "state.parquet"
            label = label_root / "label.parquet"
            for path in (market, state, label):
                path.touch()
            manifest = release_root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "release_id": "release-r1",
                        "release_root": str(release_root),
                        "market_uri": str(market),
                        "state_uri": str(state),
                        "label_release_root": str(label_root),
                        "label_uri": str(label),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            config = root / "private.env"
            config.write_text("FM_MODE=visible\nFM_CODE_COMMIT=" + "c" * 40 + "\n", encoding="utf-8")
            profile = replace(
                load_runtime_profile(complete_env(), platform_name="Linux"),
                quantlake_root=quantlake,
                market_uri=market,
                state_uri=state,
                label_uri=label,
                release_manifest_path=manifest,
                release_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
                config_path=config,
                config_hash=canonical_config_hash(config),
                code_commit="c" * 40,
            )
            identity = verify_runtime_identity(
                profile,
                repository_root=Path(__file__).parents[1],
                git_head="c" * 40,
                git_status="",
            )
            self.assertEqual(identity.release_manifest_sha256, profile.release_manifest_sha256)
            with self.assertRaisesRegex(FactorMinerError, "RUNTIME_BOUNDARY_ERROR"):
                verify_runtime_identity(
                    replace(profile, market_uri=root / "outside.parquet"),
                    repository_root=Path(__file__).parents[1],
                    git_head="c" * 40,
                    git_status="",
                )

    def test_content_identity_rejects_dirty_or_mismatched_git(self) -> None:
        """代码提交不一致或工作区不干净时不得形成正式结果。"""

        env = complete_env(mode="visible")
        profile = load_runtime_profile(env, platform_name="Linux")
        with self.assertRaisesRegex(FactorMinerError, "RUNTIME_BOUNDARY_ERROR"):
            verify_runtime_identity(
                profile,
                repository_root=Path(__file__).parents[1],
                git_head="d" * 40,
                git_status="",
            )
        with self.assertRaisesRegex(FactorMinerError, "RUNTIME_BOUNDARY_ERROR"):
            verify_runtime_identity(
                profile,
                repository_root=Path(__file__).parents[1],
                git_head="c" * 40,
                git_status=" M src/factor_miner/runtime.py",
            )

    def test_derived_release_identity_rejects_tampered_output(self) -> None:
        """派生发布必须绑定 QuantLake 上游及每个标准化输出的实际哈希。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quantlake = root / "quantlake"
            derived = root / "derived" / "r1"
            quantlake.mkdir()
            derived.mkdir(parents=True)
            upstream = quantlake / "raw.parquet"
            upstream.write_bytes(b"raw-v1")
            market = derived / "market.parquet"
            state = derived / "state.parquet"
            label = derived / "label.parquet"
            market.write_bytes(b"market-v1")
            state.write_bytes(b"state-v1")
            label.write_bytes(b"label-v1")
            manifest = derived / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "release_id": "release-r1",
                        "release_kind": "quantlake_derived_v1",
                        "transform_id": "factor_miner_standard_panel_v1",
                        "upstream_quantlake_root": str(quantlake),
                        "upstream_files": [
                            {
                                "path": str(upstream),
                                "sha256": hashlib.sha256(upstream.read_bytes()).hexdigest(),
                            }
                        ],
                        "derived_release_root": str(derived),
                        "release_root": str(derived),
                        "label_release_root": str(derived),
                        "market_uri": str(market),
                        "market_sha256": hashlib.sha256(market.read_bytes()).hexdigest(),
                        "state_uri": str(state),
                        "state_sha256": hashlib.sha256(state.read_bytes()).hexdigest(),
                        "label_uri": str(label),
                        "label_sha256": hashlib.sha256(label.read_bytes()).hexdigest(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            config = root / "private.env"
            config.write_text("FM_MODE=visible\nFM_CODE_COMMIT=" + "c" * 40 + "\n", encoding="utf-8")
            profile = replace(
                load_runtime_profile(complete_env(), platform_name="Linux"),
                artifact_root=root / "artifacts",
                quantlake_root=quantlake,
                derived_release_root=derived,
                market_uri=market,
                state_uri=state,
                label_uri=label,
                release_manifest_path=manifest,
                release_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
                config_path=config,
                config_hash=canonical_config_hash(config),
                code_commit="c" * 40,
            )
            verify_runtime_identity(
                profile,
                repository_root=Path(__file__).parents[1],
                git_head="c" * 40,
                git_status="",
            )
            market.write_bytes(b"tampered")
            with self.assertRaisesRegex(FactorMinerError, "RUNTIME_BOUNDARY_ERROR"):
                verify_runtime_identity(
                    profile,
                    repository_root=Path(__file__).parents[1],
                    git_head="c" * 40,
                    git_status="",
                )

    def test_missing_data_uri_is_rejected(self) -> None:
        """验证真实模式缺少行情、状态或标签 URI 时硬失败。"""
        env = complete_env(mode="visible")
        env["FM_LABEL_URI"] = ""
        with self.assertRaises(FactorMinerError):
            load_runtime_profile(env, platform_name="Linux")

    def test_local_real_data_path_is_accepted(self) -> None:
        """验证本地显式数据 URI 可进入真实模式。"""
        env = complete_env(mode="visible")
        env["FM_MARKET_URI"] = str(Path.cwd() / "data" / "market.parquet")
        self.assertIsNotNone(load_runtime_profile(env, platform_name="Darwin").market_uri)

    def test_data_uri_can_use_any_explicit_root(self) -> None:
        """验证真实数据 URI 不绑定 /data。"""
        env = complete_env(mode="visible")
        env["FM_STATE_URI"] = "/var/lib/factor_miner/state.parquet"
        self.assertIsNotNone(load_runtime_profile(env, platform_name="Linux").state_uri)

    def test_artifact_root_can_use_local_temporary_root(self) -> None:
        """验证真实产物目录不绑定 /data。"""
        env = complete_env(mode="visible")
        env["FM_ARTIFACT_ROOT"] = "/tmp/factor_miner_artifacts"
        profile = load_runtime_profile(env, platform_name="Darwin")
        self.assertEqual(profile.artifact_root, Path("/tmp/factor_miner_artifacts").resolve())

    def test_design_mode_allows_temporary_paths_without_real_data(self) -> None:
        """验证 design 模式可以使用临时 artifact 路径且不要求真实数据字段。"""
        with tempfile.TemporaryDirectory() as temporary_root:
            env = {"FM_MODE": "design", "FM_ARTIFACT_ROOT": temporary_root}
            profile = load_runtime_profile(env, platform_name="Darwin")
        self.assertEqual(profile.mode, ExecutionMode.DESIGN)
        self.assertIsNone(profile.market_uri)

    def test_synthetic_mode_cannot_be_used_for_real_data(self) -> None:
        """验证 synthetic 模式不能通过真实数据准入断言。"""
        with tempfile.TemporaryDirectory() as temporary_root:
            env = {"FM_MODE": "synthetic", "FM_ARTIFACT_ROOT": temporary_root}
            profile = load_runtime_profile(env, platform_name="Darwin")
        with self.assertRaises(FactorMinerError):
            assert_real_data_allowed(profile)

    def test_profile_is_immutable_and_keeps_environment_values(self) -> None:
        """验证配置不可变并保留所有 FM_ 环境变量。"""
        env = complete_env(mode="visible")
        env["FM_EXTRA_TEST_VALUE"] = "保留"
        profile = load_runtime_profile(env, platform_name="Linux")
        self.assertIsInstance(profile, RuntimeProfile)
        self.assertEqual(profile.environment["FM_EXTRA_TEST_VALUE"], "保留")
        with self.assertRaises(AttributeError):
            profile.mode = ExecutionMode.DESIGN

    def test_environment_example_lists_all_required_variables(self) -> None:
        """验证服务器环境示例列出全部必需变量。"""
        example_path = Path(__file__).parents[1] / "configs" / "company_a_share.env.example"
        example = example_path.read_text(encoding="utf-8")
        for key in complete_env():
            with self.subTest(key=key):
                self.assertIn(f"{key}=", example)
