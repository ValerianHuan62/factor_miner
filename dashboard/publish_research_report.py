"""把完整因果报告投影到美股读模型，未确定收益保持显式 NULL 并附原因。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb


def publish(root: Path, dsn: str) -> None:
    """在单个事务中校验稳定编号、存储完整报告并更新当前展示。"""
    manifest = json.loads((root / "run_manifest.json").read_text())
    if manifest["status"] != "completed_with_explicit_valuation_uncertainty" or manifest["candidate_count"] != 30:
        raise ValueError("只允许发布已完整完成的 30 槽报告")
    payloads = [json.loads(path.read_text()) for path in sorted((root / "dashboard_backtests").glob("huan*.json"))]
    if len(payloads) != 30:
        raise ValueError("回测目录候选数量不完整")
    with psycopg.connect(dsn) as connection, connection.transaction():
        if connection.execute("SELECT current_database()").fetchone()[0] != "factor_miner_us":
            raise ValueError("本次只能写入美股独立数据库")
        # 新增审计侧表，不改变用户维护的 factors 与 factor_metrics 字段结构。
        connection.execute("""CREATE TABLE IF NOT EXISTS factor_miner_internal.causal_reports (
            run_id text NOT NULL, factor_id text NOT NULL, source_candidate_id text NOT NULL,
            report jsonb NOT NULL, previous_factor_metrics jsonb, created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(run_id,factor_id))""")
        connection.execute("LOCK TABLE factor_miner_internal.factor_id_map IN SHARE ROW EXCLUSIVE MODE")
        run_id = manifest["run_id"]
        connection.execute("""INSERT INTO public.research_runs(run_id,market_id,stage,status,hypothesis_count,factor_count,started_at,finished_at,updated_at)
            VALUES(%s,'us_equity','构念、确认与持仓记账','已完成；含价值未确定持仓',10,30,now(),now(),now())""", (run_id,))
        for payload in payloads:
            fid = payload["factor_id"]
            alias = connection.execute("SELECT factor_id FROM factor_miner_internal.factor_id_map WHERE market_id='us_equity' AND source_candidate_id=%s", (payload["source_candidate_id"],)).fetchone()
            if alias != (fid,):
                raise ValueError("稳定业务编号与数据库不一致")
            metrics = payload["evaluation"]
            for field in ("ic_mean", "rank_ic_mean", "ic_std", "rank_ic_std", "ic_ir", "rank_ic_ir", "ic_hac_t", "rank_ic_hac_t"):
                if metrics.get(field) is None:
                    raise ValueError(f"{fid} 缺少 {field}")
            unknown = payload["execution_audit"]["target"]["valuation_status"] == "unresolved"
            # 完整旧指标保存在本次不可变审计中，更新后的空值有明确持仓原因。
            previous = connection.execute("SELECT to_jsonb(m) FROM public.factor_metrics m WHERE factor_id=%s", (fid,)).fetchone()
            connection.execute("INSERT INTO factor_miner_internal.causal_reports(run_id,factor_id,source_candidate_id,report,previous_factor_metrics) VALUES(%s,%s,%s,%s,%s)",
                (run_id, fid, payload["source_candidate_id"], Jsonb(payload), Jsonb(previous[0]) if previous else None))
            status = "估值未定；构念" + payload["construct_validation"]["status"] if unknown else "记账完成；构念" + payload["construct_validation"]["status"]
            hypothesis_sign = next(item["hypothesis_direction"] for item in manifest["reports"] if item["factor_id"] == fid)
            connection.execute("UPDATE public.factors SET status=%s,discovered_direction=%s,direction_relation=%s,updated_at=now() WHERE factor_id=%s AND market_id='us_equity'", (status, "正向" if payload["direction"] == "positive" else "负向", "与假设一致" if payload["direction"] == hypothesis_sign else "与假设相反", fid))
            # 旧表为全字段 NOT NULL，保留整行及原 evaluated_at，避免混合新 IC 与旧收益。
            # Dashboard 从侧表读取本次完整指标；未知实际收益在读取时显式设为 None。
            connection.execute("""UPDATE public.visible_candidate_evaluations SET run_id=%s,evaluation_scope=%s,valid_dates=%s,
                coverage_mean=%s,rank_ic_mean=%s,rank_ic_std=%s,rank_ic_ir=%s,rank_ic_hac_t=%s,raw_p_value=%s,bonferroni_p_value=%s,evaluated_at=now()
                WHERE market_id='us_equity' AND factor_id=%s""",
                (run_id, "已消费确认期；构念旁路审计；未确定收益不填造，见回测持仓与压力情景", metrics["valid_dates"], metrics["median_coverage"], *(metrics[k] for k in ("rank_ic_mean", "rank_ic_std", "rank_ic_ir", "rank_ic_hac_t", "raw_p_value", "bonferroni_p_value")), fid))
        hypotheses = {}
        for report in manifest["reports"]:
            hypotheses.setdefault(report["hypothesis_id"], report["hypothesis"])
        for identifier, hypothesis in hypotheses.items():
            connection.execute("""INSERT INTO public.research_hypotheses(run_id,hypothesis_id,title,claim,mechanism,expected_direction,
                observable_proxy,independent_verification,competing_explanations,failure_modes,falsification_path,source_description,status,reviewed_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())""",
                (run_id, identifier, identifier + "：" + hypothesis["claim"], hypothesis["claim"], hypothesis["mechanism"], {"positive": "正向", "negative": "负向", "neutral": "中性"}[hypothesis["expected_sign"]],
                 hypothesis["observable_proxy"], hypothesis["independent_verification"], "；".join(hypothesis["competing_explanations"]),
                 "；".join(hypothesis["failure_modes"]), hypothesis["falsification_path"], "原假设来源：" + "；".join(hypothesis["source_refs"]), "原假设已完成重算；经济机制未独立验证"))
    print(json.dumps({"status": "published", "factor_count": len(payloads), "database": "factor_miner_us", "run_id": run_id}, ensure_ascii=False))


def main() -> None:
    """显式运行目录和连接入口，不推断其他市场数据库。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_root", type=Path)
    parser.add_argument("--dsn", default="dbname=factor_miner_us")
    args = parser.parse_args()
    publish(args.report_root, args.dsn)
    publish_quality(args.report_root, args.dsn)


def publish_quality(root: Path, dsn: str) -> None:
    """以独立审计附录发布后验数据核查，不改写不可变计算报告。"""
    quality_path = root / "data_quality_audit.json"
    if not quality_path.is_file():
        return
    quality = json.loads(quality_path.read_text())
    run_id = json.loads((root / "run_manifest.json").read_text())["run_id"]
    with psycopg.connect(dsn) as connection, connection.transaction():
        if connection.execute("SELECT current_database()").fetchone()[0] != "factor_miner_us":
            raise ValueError("仅允许独立美股数据库")
        connection.execute("""CREATE TABLE IF NOT EXISTS factor_miner_internal.run_quality_audits (
            run_id text PRIMARY KEY, report jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())""")
        connection.execute("INSERT INTO factor_miner_internal.run_quality_audits(run_id,report) VALUES(%s,%s)", (run_id, Jsonb(quality)))
        if quality["status"] == "review_required":
            connection.execute("""UPDATE public.factors SET status='数据待核；' || status, updated_at=now()
                WHERE market_id='us_equity' AND factor_id IN (
                    SELECT factor_id FROM factor_miner_internal.causal_reports WHERE run_id=%s)""", (run_id,))
            connection.execute("UPDATE public.research_runs SET status='计算完成；数据待核及估值未定',updated_at=now() WHERE run_id=%s", (run_id,))


if __name__ == "__main__":
    main()
