"""FaVOR 跨市场 PostgreSQL 投影适配器。"""
from pathlib import Path
from factor_miner.favor_store import projection_payload
from factor_miner.favor_workflow import file_sha

DDL = """
CREATE TABLE IF NOT EXISTS public.favor_trial_records (
 market_id text NOT NULL CHECK(market_id IN ('a_share','us_equity')),
 plan_sha256 text NOT NULL, trial_id text NOT NULL, source_candidate_id text,
 status text NOT NULL, reason text NOT NULL, diagnostic_family_size integer,
 source_path text NOT NULL, source_sha256 text NOT NULL,
 PRIMARY KEY(market_id,plan_sha256,trial_id));
ALTER TABLE public.favor_trial_records ALTER COLUMN diagnostic_family_size DROP NOT NULL;
CREATE TABLE IF NOT EXISTS public.favor_factors (
 market_id text NOT NULL CHECK(market_id IN ('a_share','us_equity')),
 plan_sha256 text NOT NULL, factor_id text NOT NULL, source_candidate_id text NOT NULL,
 qualification text NOT NULL CHECK(qualification='retained_component'),
 standalone_statistical_pass boolean NOT NULL, report jsonb NOT NULL,
 source_path text NOT NULL, source_sha256 text NOT NULL,
 PRIMARY KEY(market_id,plan_sha256,factor_id), UNIQUE(market_id,plan_sha256,source_candidate_id));
CREATE TABLE IF NOT EXISTS public.favor_combinations (
 market_id text NOT NULL CHECK(market_id IN ('a_share','us_equity')),
 plan_sha256 text NOT NULL, combination_id text NOT NULL, member_factor_ids text[] NOT NULL,
 report jsonb NOT NULL, source_sha256 text NOT NULL,
 PRIMARY KEY(market_id,plan_sha256,combination_id));
"""


def publish_favor(root: Path, dsn: str) -> dict:
    """复用全库稳定 huan 编号；单事务投影，失败/待审只有精简记录。"""
    import psycopg
    from psycopg.types.json import Jsonb
    summary, kept, trials = projection_payload(root)
    if summary["run_kind"] != "research":
        raise ValueError("合成演示不能发布到研究数据库")
    market, plan_hash = summary["market_id"], summary["plan_sha256"]
    with psycopg.connect(dsn) as connection, connection.transaction():
        # 使用既有全库编号表，绝不按本次运行重新从 huan001 编号。
        connection.execute("LOCK TABLE factor_miner_internal.factor_id_map IN SHARE ROW EXCLUSIVE MODE")
        connection.execute(DDL)
        maximum = connection.execute("""SELECT COALESCE(MAX(value),0) FROM (
            SELECT substring(factor_id FROM 5)::integer AS value FROM factor_miner_internal.factor_id_map WHERE factor_id ~ '^huan[0-9]{3,}$'
            UNION ALL SELECT substring(factor_id FROM 5)::integer FROM public.factors WHERE factor_id ~ '^huan[0-9]{3,}$') AS ids""").fetchone()[0]
        aliases = {}
        for item in kept:
            source = item["record"]["source_candidate_id"]
            row = connection.execute("SELECT factor_id FROM factor_miner_internal.factor_id_map WHERE market_id=%s AND source_candidate_id=%s", (market, source)).fetchone()
            if row:
                alias = row[0]
            else:
                maximum += 1
                alias = f"huan{maximum:03d}"
                connection.execute("INSERT INTO factor_miner_internal.factor_id_map (market_id,source_candidate_id,factor_id) VALUES (%s,%s,%s)", (market,source,alias))
            aliases[item["record"]["trial_id"]] = alias
        for record in trials:
            prior = connection.execute("SELECT source_sha256 FROM public.favor_trial_records WHERE market_id=%s AND plan_sha256=%s AND trial_id=%s", (market,plan_hash,record["trial_id"])).fetchone()
            if prior and prior[0] != record["source_sha256"]:
                raise ValueError("已有投影对应不同的试验记录")
            connection.execute("INSERT INTO public.favor_trial_records VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (market,plan_hash,record["trial_id"],record["source_candidate_id"],record["status"],record["reason"],summary["diagnostic_family_size"],record["source_path"],record["source_sha256"]))
        for item in kept:
            record, report = item["record"], item["report"]
            connection.execute("INSERT INTO public.favor_factors VALUES (%s,%s,%s,%s,'retained_component',%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (market,plan_hash,aliases[record["trial_id"]],record["source_candidate_id"],report["standalone_statistical_pass"],Jsonb(report),record["source_path"],record["source_sha256"]))
        for cid, report in summary["combinations"].items():
            if report["status"] == "retained_combination":
                connection.execute("INSERT INTO public.favor_combinations VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (market,plan_hash,cid,[aliases[t] for t in report["members"]],Jsonb(report),file_sha(root/"summary.json")))
        for table, expected in (("favor_trial_records",len(trials)),("favor_factors",len(kept)),("favor_combinations",summary["retained_combinations"])):
            count = connection.execute(f"SELECT count(*) FROM public.{table} WHERE market_id=%s AND plan_sha256=%s", (market,plan_hash)).fetchone()[0]
            if count != expected:
                raise ValueError("FaVOR 投影数量核验失败，事务回滚")
    from factor_miner.favor_workflow import load_plan
    from factor_miner.favor_schema import FavorRegimePlan
    plan = load_plan(root)
    if isinstance(plan,FavorRegimePlan) and aliases:
        from factor_miner.regime import RegimeRecord, RegimeManifest, save_manifest
        from factor_miner_pg.regime_store import project_regimes
        records=[]
        sources={str((root/'plan.json').resolve()):file_sha(root/'plan.json')}
        for trial_id,alias in aliases.items():
            path=root/'submissions'/trial_id/'regime_record.json'
            record=RegimeRecord.model_validate_json(path.read_text())
            records.append(record.model_copy(update={'factor_id':alias}))
            sources[str(path.resolve())]=file_sha(path)
        manifest=RegimeManifest(market_id=market,context_id=plan_hash,records=records,source_files=sources)
        path=root/'regime_manifest.json'
        save_manifest(manifest,path)
        project_regimes(path,dsn,market)
    return dict(status="published", market_id=market, retained_factors=len(kept), trial_records=len(trials),
                combinations=summary["retained_combinations"], aliases=aliases)
