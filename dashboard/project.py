"""把服务器已发布运行投影到 PostgreSQL Dashboard 读模型。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from factor_miner.dashboard_projection import project_run_artifacts
from dashboard.market_profiles import dashboard_dsn
from dashboard.pg_store import PostgresDashboardStore


def main() -> None:
    """执行一次只读产物核验后的幂等数据库投影。"""

    parser = argparse.ArgumentParser(description="投影已发布 Factor Miner 运行")
    parser.add_argument("run_id")
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--market-id", default="a_share")
    args = parser.parse_args()
    dsn = dashboard_dsn(args.market_id)
    if not dsn:
        raise SystemExit(f"缺少 PostgreSQL 连接 FM_DASHBOARD_DSN_{args.market_id.upper()}")
    snapshot = project_run_artifacts(
        args.artifact_root,
        args.run_id,
        PostgresDashboardStore(dsn, market_id=args.market_id),
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
