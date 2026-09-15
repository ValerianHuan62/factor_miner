"""将开发期可见候选投影到指定市场的 PostgreSQL 读模型。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dashboard.pg_store import PostgresDashboardStore
from dashboard.market_profiles import dashboard_dsn
from dashboard.visible_campaign import load_visible_campaign_rows


def main() -> None:
    """执行显式市场归属的可见候选投影。"""

    parser = argparse.ArgumentParser(description="投影开发期可见候选")
    parser.add_argument("--market-id", required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--candidate-state-root", type=Path, required=True)
    parser.add_argument("--pre-registration", type=Path, required=True)
    parser.add_argument("--horizon-days", type=int, required=True)
    args = parser.parse_args()
    dsn = dashboard_dsn(args.market_id)
    if not dsn:
        raise SystemExit(f"缺少 PostgreSQL 连接 FM_DASHBOARD_DSN_{args.market_id.upper()}")
    run_id, rows = load_visible_campaign_rows(
        run_root=args.run_root,
        candidate_state_root=args.candidate_state_root,
        pre_registration_path=args.pre_registration,
        horizon_days=args.horizon_days,
    )
    aliases = PostgresDashboardStore(dsn, market_id=args.market_id).replace_visible_candidate_evaluations(
        run_id,
        rows,
    )
    print(json.dumps({"market_id": args.market_id, "run_id": run_id, "aliases": aliases}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
