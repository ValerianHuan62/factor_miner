"""独立投影组合研究代表，原统计留库表与试验记录保持原样。"""
from pathlib import Path
import argparse
import json

import psycopg
from psycopg.types.json import Jsonb

from factor_miner.ridge_strategy import file_hash
from factor_miner.research_report import write_json


def publish(path: Path, receipt_root: Path, dsn: str = 'dbname=factor_miner_us connect_timeout=5') -> dict:
    """仅投影通过独立研究资格的代表，永久复用已分配编号。"""
    pool = json.loads(path.read_text())
    if pool['schema_version'] != 'research-pool-dashboard-v1' or pool['market_id'] != 'us_equity':
        raise ValueError('只允许明确的美股组合研究投影')
    kept = {fid:e for fid,e in pool['by_factor'].items() if e['status']=='research_input'}
    for fid, entry in kept.items():
        if file_hash(Path(entry['report_path'])) != entry['report_sha256']:
            raise ValueError(f'{fid} 原报告发生变化')
        payload = json.loads(Path(entry['report_path']).read_text())
        if payload['source_candidate_id'] != entry['source_candidate_id']:
            raise ValueError('原报告候选身份不符')
    receipt_root.mkdir(parents=True, exist_ok=False)
    with psycopg.connect(dsn) as conn, conn.transaction():
        if conn.execute('SELECT current_database()').fetchone()[0] != 'factor_miner_us':
            raise ValueError('拒绝写入其他数据库')
        conn.execute('SELECT pg_advisory_xact_lock(190908)')
        aliases = dict(conn.execute("SELECT source_candidate_id,factor_id FROM factor_miner_internal.factor_id_map WHERE market_id='us_equity'").fetchall())
        if any(aliases.get(e['source_candidate_id']) != fid for fid,e in kept.items()):
            raise ValueError('稳定编号映射不一致')
        before = [conn.execute(f'SELECT count(*) FROM public.{table}').fetchone()[0]
                  for table in ('screened_factors','factor_trial_records')]
        conn.execute("""CREATE TABLE IF NOT EXISTS public.factor_research_pool (
            market_id text NOT NULL CHECK(market_id='us_equity'), pool_id text NOT NULL,
            factor_id text NOT NULL, source_candidate_id text NOT NULL,
            factor_name text NOT NULL, original_status text NOT NULL,
            original_statistical_pass boolean NOT NULL, qualification text NOT NULL CHECK(qualification='research_input'),
            review jsonb NOT NULL, source_path text NOT NULL, source_sha256 text NOT NULL,
            PRIMARY KEY(market_id,pool_id,factor_id), UNIQUE(market_id,pool_id,source_candidate_id))""")
        for fid,e in kept.items():
            conn.execute("""INSERT INTO public.factor_research_pool VALUES
                ('us_equity',%s,%s,%s,%s,%s,%s,'research_input',%s,%s,%s)""",
                (pool['pool_id'],fid,e['source_candidate_id'],e['name'],e['previous_status'],
                 e['original_statistical_pass'],Jsonb(e),str(path),file_hash(path)))
        after = [conn.execute(f'SELECT count(*) FROM public.{table}').fetchone()[0]
                 for table in ('screened_factors','factor_trial_records')]
        actual = conn.execute('SELECT factor_id FROM public.factor_research_pool WHERE pool_id=%s ORDER BY factor_id', (pool['pool_id'],)).fetchall()
        if before != after or [x[0] for x in actual] != sorted(kept):
            raise ValueError('投影核验失败')
    receipt = dict(status='published', pool_id=pool['pool_id'], research_representatives=len(kept),
        restored_from_rejected=sum(e['previous_status']=='rejected' for e in kept.values()),
        screened_count=before[0], historical_trial_count=before[1], source_sha256=file_hash(path))
    write_json(receipt_root/'receipt.json', receipt)
    return receipt


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pool',type=Path,required=True)
    parser.add_argument('--receipt-root',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(publish(args.pool,args.receipt_root),ensure_ascii=False))
