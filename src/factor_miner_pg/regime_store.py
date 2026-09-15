"""状态假设的增量 PostgreSQL 投影，文件清单决定当前显示版本。"""
from pathlib import Path
from factor_miner.canonical import sha256_json
from factor_miner.regime import RegimeRecord, load_manifest, record_fields

DDL = """
CREATE TABLE IF NOT EXISTS public.factor_regime_hypotheses (
 market_id text NOT NULL CHECK(market_id IN ('a_share','us_equity')),
 source_candidate_id text NOT NULL, context_id text NOT NULL, record_id text NOT NULL,
 factor_id text NOT NULL, spec_sha256 text NOT NULL, expected_regime_label text NOT NULL,
 failure_condition_summary text NOT NULL, record jsonb NOT NULL,
 selected_validation_id text, is_current boolean NOT NULL DEFAULT false,
 manifest_path text NOT NULL, manifest_sha256 text NOT NULL,
 PRIMARY KEY(market_id,source_candidate_id,context_id,record_id));
CREATE UNIQUE INDEX IF NOT EXISTS factor_regime_one_current
 ON public.factor_regime_hypotheses(market_id,source_candidate_id,context_id) WHERE is_current;
CREATE TABLE IF NOT EXISTS public.factor_regime_validations (
 market_id text NOT NULL, source_candidate_id text NOT NULL, context_id text NOT NULL,
 validation_id text NOT NULL, spec_sha256 text NOT NULL, validation_status text NOT NULL,
 report jsonb NOT NULL,
 PRIMARY KEY(market_id,source_candidate_id,context_id,validation_id));
CREATE OR REPLACE VIEW public.factor_regime_current AS
 SELECT h.market_id,h.source_candidate_id,h.factor_id,h.context_id,h.spec_sha256,
 h.expected_regime_label,h.failure_condition_summary,
 COALESCE(v.validation_status,CASE WHEN NOT (h.record->'spec'->>'has_claim')::boolean THEN '未开展'
 WHEN NOT (h.record->'spec'->>'data_available')::boolean THEN '待数据' ELSE '待检验' END) AS regime_validation_status,
 h.record, v.report AS validation, h.manifest_sha256
 FROM public.factor_regime_hypotheses h LEFT JOIN public.factor_regime_validations v
 ON v.market_id=h.market_id AND v.source_candidate_id=h.source_candidate_id AND v.context_id=h.context_id
 AND v.validation_id=h.selected_validation_id AND v.spec_sha256=h.spec_sha256 WHERE h.is_current;
"""


def project_regimes(path: Path, dsn: str, market_id: str) -> dict:
    """只投影已校验清单；稳定编号缺失或冲突即回滚。"""
    import psycopg
    from psycopg.types.json import Jsonb
    from factor_miner.regime import file_sha
    manifest = load_manifest(path, market_id)
    with psycopg.connect(dsn) as conn:
        conn.execute(DDL)
        conn.execute('LOCK TABLE public.factor_regime_hypotheses IN SHARE ROW EXCLUSIVE MODE')
        aliases = dict(conn.execute('SELECT source_candidate_id,factor_id FROM factor_miner_internal.factor_id_map WHERE market_id=%s', (market_id,)).fetchall())
        for r in manifest.records:
            if aliases.get(r.source_candidate_id) != r.factor_id:
                raise ValueError('状态投影缺少对应稳定编号或身份冲突：'+r.factor_id)
        # 当前选择来自完整上下文清单，不按时间或统计表现猜测。
        conn.execute('UPDATE public.factor_regime_hypotheses SET is_current=false WHERE market_id=%s AND context_id=%s', (market_id,manifest.context_id))
        for r in manifest.records:
            payload = r.model_dump(mode='json')
            rid = sha256_json(payload)
            vid = sha256_json(payload['validation']) if r.validation else None
            key = (market_id, r.source_candidate_id, r.context_id)
            fields = record_fields(r)
            if r.validation:
                previous = conn.execute('SELECT report FROM public.factor_regime_validations WHERE market_id=%s AND source_candidate_id=%s AND context_id=%s AND validation_id=%s', (*key,vid)).fetchone()
                if previous and previous[0] != payload['validation']:
                    raise ValueError('检验身份内容冲突')
                conn.execute('INSERT INTO public.factor_regime_validations VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                    (*key,vid,r.spec.identity,fields['regime_validation_status'],Jsonb(payload['validation'])))
            previous = conn.execute('SELECT record FROM public.factor_regime_hypotheses WHERE market_id=%s AND source_candidate_id=%s AND context_id=%s AND record_id=%s', (*key,rid)).fetchone()
            if previous and previous[0] != payload:
                raise ValueError('状态登记身份内容冲突')
            conn.execute('''INSERT INTO public.factor_regime_hypotheses VALUES
             (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s)
             ON CONFLICT(market_id,source_candidate_id,context_id,record_id)
             DO UPDATE SET is_current=true,manifest_path=EXCLUDED.manifest_path,manifest_sha256=EXCLUDED.manifest_sha256''',
             (*key,rid,r.factor_id,r.spec.identity,fields['expected_regime_label'],r.spec.failure_condition,Jsonb(payload),vid,str(path.resolve()),file_sha(path)))
        count = conn.execute('SELECT count(*) FROM public.factor_regime_current WHERE market_id=%s AND context_id=%s',(market_id,manifest.context_id)).fetchone()[0]
        if count != len(manifest.records):
            raise ValueError('当前状态投影数量不符')
    return dict(status='projected', market_id=market_id, context_id=manifest.context_id, records=count, manifest_sha256=file_sha(path))


def attach_pg_records(connection, rows: list[dict], market_id: str, context_id: str | None) -> list[dict]:
    """必须显式选择评价上下文；未配置时仅返回未登记标签。"""
    if not context_id:
        return [{**row,**record_fields(None),'regime_record':None} for row in rows]
    if connection.execute("SELECT to_regclass('public.factor_regime_current')").fetchone()[0] is None:
        raise ValueError('已配置状态上下文但尚未投影 PostgreSQL')
    records = [RegimeRecord.model_validate(row[0]) for row in connection.execute(
        'SELECT record FROM public.factor_regime_current WHERE market_id=%s AND context_id=%s', (market_id,context_id)).fetchall()]
    if not records:
        raise ValueError('配置的状态上下文没有已发布记录')
    from factor_miner.regime import RegimeManifest, attach_records
    # 旧指标视图没有候选身份时，只能通过正式稳定映射补充。
    aliases = dict(connection.execute('SELECT factor_id,source_candidate_id FROM factor_miner_internal.factor_id_map WHERE market_id=%s',(market_id,)).fetchall())
    enriched = [{**row,'source_candidate_id':row.get('source_candidate_id') or aliases.get(row['factor_id'])} for row in rows]
    return attach_records(enriched, RegimeManifest(market_id=market_id,context_id=context_id,records=records,source_files={}))
