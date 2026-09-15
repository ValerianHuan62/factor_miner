"""生成 Dashboard 使用的聚合真实美股单因子回测产物。"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

from dashboard.real_backtest import build_real_us_backtest


def main() -> None:
    """从显式真实输入生成不含个股持仓的 Dashboard 产物。"""

    parser = argparse.ArgumentParser(description="生成真实美股单因子 Dashboard 回测")
    parser.add_argument("--factor", type=Path, required=True)
    parser.add_argument("--market", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-candidate-id", required=True)
    parser.add_argument("--factor-id", required=True)
    parser.add_argument("--factor-name", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--holding-sessions", type=int, default=5)
    parser.add_argument("--group-count", type=int, default=10)
    parser.add_argument("--base-cost-bps", type=float, default=10.0)
    parser.add_argument("--rank-ic-mean", type=float, required=True)
    parser.add_argument("--rank-ic-hac-t", type=float, required=True)
    args = parser.parse_args()
    payload = build_real_us_backtest(
        factor_path=args.factor,
        market_path=args.market,
        state_path=args.state,
        manifest_path=args.manifest,
        output_path=args.output,
        source_candidate_id=args.source_candidate_id,
        factor_id=args.factor_id,
        factor_name=args.factor_name,
        start_date=args.start,
        end_date=args.end,
        holding_sessions=args.holding_sessions,
        group_count=args.group_count,
        base_cost_bps=args.base_cost_bps,
        rank_ic_mean=args.rank_ic_mean,
        rank_ic_hac_t=args.rank_ic_hac_t,
    )
    print(json.dumps({
        "output": str(args.output),
        "factor_id": payload["factor_id"],
        "observations": len(payload["daily_rows"]),
        "scope": payload["scope"],
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
