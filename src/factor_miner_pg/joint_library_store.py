"""代表库的可重建成员投影；原候选指标和编号保持不变。"""
from pathlib import Path

from factor_miner.joint_library import load_library
from factor_miner.joint_study import digest


def publish_library(root: Path, dsn: str) -> dict:
    import psycopg
    value = load_library(root)
    version = digest(root/'library.json')
    with psycopg.connect(dsn) as conn, conn.transaction():
        if conn.execute('SELECT current_database()').fetchone()[0] != 'factor_miner':
            raise ValueError('拒绝写入其他市场数据库')
        conn.execute('LOCK TABLE factor_miner_internal.factor_id_map IN SHARE ROW EXCLUSIVE MODE')
        conn.execute('''CREATE TABLE IF NOT EXISTS public.factor_library_members (
            market_id text NOT NULL CHECK(market_id='a_share'), library_id text NOT NULL,
            factor_id text NOT NULL, source_candidate_id text NOT NULL,
            role text NOT NULL CHECK(role IN ('研究代表','同类替补')), representative_id text NOT NULL,
            source_path text NOT NULL, PRIMARY KEY(market_id,library_id,factor_id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS public.factor_library_active (
            market_id text PRIMARY KEY CHECK(market_id='a_share'), library_id text NOT NULL)''')
        for row in value['members']:
            identity = conn.execute("SELECT factor_id FROM factor_miner_internal.factor_id_map WHERE market_id='a_share' AND source_candidate_id=%s", (row['source_candidate_id'],)).fetchone()
            original = conn.execute("SELECT DISTINCT qualification FROM public.favor_research_candidates WHERE market_id='a_share' AND source_candidate_id=%s AND factor_id=%s", (row['source_candidate_id'],row['factor_id'])).fetchall()
            if identity != (row['factor_id'],) or original != [('research_candidate_pending_confirmation',)]:
                raise ValueError('代表库成员与原候选身份或资格不符')
            conn.execute('INSERT INTO public.factor_library_members VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                ('a_share',version,row['factor_id'],row['source_candidate_id'],row['role'],row['representative'],str(root.resolve())))
        actual = conn.execute('SELECT factor_id,source_candidate_id,role,representative_id FROM public.factor_library_members WHERE library_id=%s ORDER BY factor_id', (version,)).fetchall()
        expected = sorted((r['factor_id'],r['source_candidate_id'],r['role'],r['representative']) for r in value['members'])
        if actual != expected:
            raise ValueError('代表库投影内容不一致')
        conn.execute("INSERT INTO public.factor_library_active VALUES ('a_share',%s) ON CONFLICT(market_id) DO UPDATE SET library_id=EXCLUDED.library_id", (version,))
    return dict(status='published', library_id=version, representatives=sum(r['role']=='研究代表' for r in value['members']), reserves=sum(r['role']=='同类替补' for r in value['members']))
