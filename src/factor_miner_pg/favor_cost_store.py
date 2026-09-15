"""A 股研究候选投影，待确认资格与正式因子表分开保存。"""
import json
import math
from pathlib import Path
from factor_miner.favor_cost_review import load_cost_review, candidate_formula
from factor_miner.favor_workflow import file_sha
from factor_miner.research_report import write_json


def publish_cost_review(root: Path, dsn: str) -> dict:
    import psycopg
    from psycopg.types.json import Jsonb
    summary = load_cost_review(root)
    if summary['market_id'] != 'a_share': raise ValueError('本次研究候选投影仅授权A股')
    kept = [x for x in summary['results'] if x['admission']['eligible']]
    formulas={row['trial_id']:candidate_formula(row)[1] for row in kept}
    for row in kept:
        for key in ('ic_mean','rank_ic_mean','ic_std','rank_ic_std','ic_ir','rank_ic_ir','ic_hac_t','rank_ic_hac_t'):
            if not isinstance(row['discovery'].get(key),(int,float)) or not math.isfinite(row['discovery'][key]):
                raise ValueError('研究候选IC指标不完整')
        if row['admission']['formal_pass']: raise ValueError('不得伪装正式确认')
    version = file_sha(root/'protocol.json'); aliases = {}
    with psycopg.connect(dsn) as conn, conn.transaction():
        if conn.execute('SELECT current_database()').fetchone()[0] != 'factor_miner':
            raise ValueError('拒绝写入其他市场数据库')
        conn.execute('LOCK TABLE factor_miner_internal.factor_id_map IN SHARE ROW EXCLUSIVE MODE')
        conn.execute('''CREATE TABLE IF NOT EXISTS public.favor_research_candidates (
            market_id text NOT NULL CHECK(market_id='a_share'), review_id text NOT NULL,
            factor_id text NOT NULL, source_candidate_id text NOT NULL, trial_id text NOT NULL,
            qualification text NOT NULL CHECK(qualification='research_candidate_pending_confirmation'),
            round_trip_cost_bps double precision NOT NULL, ic_mean double precision NOT NULL,
            rank_ic_mean double precision NOT NULL, original_statistical_pass boolean,
            annualized_return double precision, reference_annualized_return double precision NOT NULL,
            report jsonb NOT NULL, source_path text NOT NULL, source_sha256 text NOT NULL,
            PRIMARY KEY(market_id,review_id,factor_id), UNIQUE(market_id,review_id,source_candidate_id))''')
        maximum = conn.execute("""SELECT COALESCE(MAX(value),0) FROM (
            SELECT substring(factor_id FROM 5)::integer AS value FROM factor_miner_internal.factor_id_map WHERE factor_id ~ '^huan[0-9]{3,}$'
            UNION ALL SELECT substring(factor_id FROM 5)::integer FROM public.factors WHERE factor_id ~ '^huan[0-9]{3,}$') AS ids""").fetchone()[0]
        for row in kept:
            source = row['source_candidate_id']
            found = conn.execute("SELECT factor_id FROM factor_miner_internal.factor_id_map WHERE market_id='a_share' AND source_candidate_id=%s",(source,)).fetchone()
            if found: alias = found[0]
            else:
                maximum += 1; alias = f'huan{maximum:03d}'
                conn.execute("INSERT INTO factor_miner_internal.factor_id_map (market_id,source_candidate_id,factor_id) VALUES ('a_share',%s,%s)",(source,alias))
            aliases[row['trial_id']] = alias
            path = root/row['trial_id']/'report.json'; digest = file_sha(path)
            prior = conn.execute("SELECT source_sha256 FROM public.favor_research_candidates WHERE market_id='a_share' AND review_id=%s AND factor_id=%s",(version,alias)).fetchone()
            if prior and prior[0] != digest: raise ValueError('已发布候选身份变化')
            conn.execute("""INSERT INTO public.favor_research_candidates VALUES
                ('a_share',%s,%s,%s,%s,'research_candidate_pending_confirmation',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING""",(version,alias,source,row['trial_id'],summary['round_trip_cost_bps'],
                row['discovery']['ic_mean'],row['discovery']['rank_ic_mean'],row['standalone_statistical_pass'],
                (row['metrics']['actual'] or {}).get('annualized_return'),row['metrics']['reference']['annualized_return'],
                Jsonb(dict(row,expression=candidate_formula(row)[0],formula=formulas[row['trial_id']])),str(path),digest))
        count=conn.execute("SELECT count(*) FROM public.favor_research_candidates WHERE review_id=%s",(version,)).fetchone()[0]
        if count != len(kept): raise ValueError('投影数量不符')
    receipt=dict(status='published',market_id='a_share',review_id=version,summary_sha256=file_sha(root/'summary.json'),
                 aliases=aliases,formulas=formulas,research_candidates=len(kept),formal_candidates=0)
    path=root/'publication.json'
    if path.exists() and json.loads(path.read_text()) != receipt: raise ValueError('既有发布回执不一致')
    if not path.exists(): write_json(path,receipt)
    return receipt
