"""FaVOR 的跨市场 PostgreSQL 读模型：仅保留组成因子和组合保存完整报告。"""
from __future__ import annotations

import json
import math
from pathlib import Path

from factor_miner.favor_workflow import load_plan, file_sha


def projection_payload(root: Path) -> tuple[dict, list[dict], list[dict]]:
    """投影只接受已完成、身份一致且指标完整的不可变运行。"""
    plan = load_plan(root)
    receipt = json.loads((root/"completion.json").read_text())
    if receipt["summary_sha256"] != file_sha(root/"summary.json") or receipt["strategy_freeze_sha256"] != file_sha(root/"strategy_freeze.json"):
        raise ValueError("FaVOR 结果或阈值冻结记录发生变化")
    summary = json.loads((root/"summary.json").read_text())
    if plan.version == 'favor-exploration-v1':
        if (receipt.get('exploration_freeze_sha256') != file_sha(root/'exploration_freeze.json')
                or summary['retained_components'] or summary['retained_combinations']
                or summary.get('test_evaluated') is not False):
            raise ValueError('探索记录身份损坏或被错误升级为正式通过')
    if summary["status"] != "completed" or summary["market_id"] != plan.market_id:
        raise ValueError("FaVOR 未完成或市场不符")
    frozen = json.loads((root/"strategy_freeze.json").read_text())
    kept, trials = [], []
    for trial_id, report in summary["factors"].items():
        record = dict(trial_id=trial_id, source_candidate_id=report.get("source_candidate_id"), status=report["status"],
            reason=report.get("reason", report.get("qualification", report["status"])),
            source_path=str(root/"trials"/f"{trial_id}.json"), source_sha256=file_sha(root/"trials"/f"{trial_id}.json"))
        if plan.search_budget.get('historical_budget_status') == 'unrecoverable':
            record['reason'] += '；历史预算不可恢复，未作完整多重检验通过判断'
        trials.append(record)
        if report["status"] != "retained_component":
            continue
        if trial_id not in frozen["retained_trials"] or not report["empirical"]["passed"] or report["synthetic"]["status"] != "基础检验符合":
            raise ValueError("未通过统一构念筛选的候选不能入库")
        for key in ("ic_mean", "rank_ic_mean", "ic_std", "rank_ic_std", "ic_ir", "rank_ic_ir", "ic_hac_t", "rank_ic_hac_t"):
            if not isinstance(report["test_ic"].get(key), (float,int)) or not math.isfinite(report["test_ic"][key]):
                raise ValueError(f"保留候选缺少完整 {key}")
        portfolio = report["portfolio"]
        actual = portfolio["metrics"]["actual"]
        if actual is None:
            if not portfolio["unresolved_positions"]:
                raise ValueError("缺少实际投资指标但没有未确定持仓原因")
        else:
            for key in ("annualized_return", "max_drawdown", "sharpe", "information_ratio"):
                if key == "information_ratio" and portfolio["benchmark_unresolved_positions"]:
                    continue
                if not isinstance(actual.get(key), (float,int)) or not math.isfinite(actual[key]):
                    raise ValueError(f"保留候选缺少完整 {key}")
        if portfolio["win_rate"] is None and not (portfolio["unresolved_positions"] or portfolio["opposite_unresolved_positions"]):
            raise ValueError("缺少按冻结价差计算的胜率")
        kept.append(dict(record=record, report=report))
    if len(kept) != summary["retained_components"]:
        raise ValueError("留库数与完成汇总不一致")
    return summary, kept, trials
