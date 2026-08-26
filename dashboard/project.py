"""把服务器已发布运行投影到 PostgreSQL Dashboard 读模型。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from factor_miner.dashboard_projection import project_run_artifacts
from dashboard.pg_store import PostgresDashboardStore


def main() -> None:
    """执行一次只读产物核验后的幂等数据库投影。"""

    parser = argparse.ArgumentParser(description="投影已发布 Factor Miner 运行")
    parser.add_argument("run_id")
    parser.add_argument("--artifact-root", type=Path, required=True)
    args = parser.parse_args()
    dsn = os.environ.get("FM_DASHBOARD_DSN", "")
    if not dsn:
        raise SystemExit("缺少服务器私有环境 FM_DASHBOARD_DSN")
    snapshot = project_run_artifacts(
        args.artifact_root,
        args.run_id,
        PostgresDashboardStore(dsn),
    )
    print(json.dumps(
        {
            "run_id": snapshot.run_id,
            "snapshot_sha256": snapshot.snapshot_sha256,
            "artifact_manifest_sha256": snapshot.artifact_manifest_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
    ))


if __name__ == "__main__":
    main()
