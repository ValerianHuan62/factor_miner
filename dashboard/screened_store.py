"""筛选留库的 PostgreSQL 投影；失败候选仅保存精简索引，文件账本保持完整。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from factor_miner.research_report import write_json
from factor_miner.ledger import JsonlLedger

KEPT = {'retained', 'reserve', 'watch'}
IC_FIELDS = ('ic_mean', 'rank_ic_mean', 'ic_std', 'rank_ic_std', 'ic_ir', 'rank_ic_ir', 'ic_hac_t', 'rank_ic_hac_t')


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metric_rows(connection) -> list[dict]:
    """旧仪表盘指标入口改读完整留库表，未知收益保持空值。"""
    result = []
    for fid, payload, entry, updated in connection.execute("SELECT factor_id,report,review,updated_at FROM public.screened_factors WHERE market_id='us_equity' ORDER BY factor_id").fetchall():
        unknown = bool(payload.get('unresolved_positions'))
        metrics = payload['reference_metrics']
        evaluation = payload['evaluation']
        row = dict(market_id='us_equity', factor_id=fid, hypothesis=entry['display_name'],
            mechanism=entry.get('semantic_review','机制尚未独立验证'), formula=entry.get('formula',''),
            calculation=payload.get('construct_validation',{}).get('condition',{}).get('observation',''),
            discovered_direction=payload['direction'], direction_relation='原假设方向', status=entry['label'],
            horizon_days=5, valid_dates=evaluation['valid_dates'], coverage_mean=evaluation['median_coverage'],
            **{k:evaluation[k] for k in IC_FIELDS},
            **{k:None if unknown else metrics[k] for k in ('annualized_return','max_drawdown','sharpe','information_ratio')},
            has_portfolio=not unknown, evaluation_scope=payload['scope'], run_id=None, evaluated_at=updated)
        if payload.get('benchmark_unresolved_positions'):
            row['information_ratio'] = None
        result.append(row)
    return result


def build_projection(profile: dict, trials_root: Path) -> tuple[list[dict], list[dict], dict]:
    """筛选名录与报告身份必须一致；缺评审、统计失败不能进入留库表。"""
    if profile['market_id'] != 'us_equity':
        raise ValueError('当前迁移仅适用于用户授权的美股数据库')
    review_path = Path(profile['library_review_path'])
    review = json.loads(review_path.read_text())
    if review['market_id'] != profile['market_id'] or review['schema_version'] != 'library-review-v1':
        raise ValueError('复核市场或版本不一致')
    paths = {}
    for folder in profile['backtest_roots']:
        for path in sorted(Path(folder).glob('huan*.json')):
            paths[path.stem] = path
    records, kept = {}, []
    for fid, entry in review['by_factor'].items():
        path = paths[fid]
        if sha(path) != entry['report_sha256']:
            raise ValueError(f'{fid} 报告内容变化')
        payload = json.loads(path.read_text())
        if payload['source_candidate_id'] != entry['source_candidate_id'] or payload['market_id'] != 'us_equity':
            raise ValueError('报告候选身份或市场不一致')
        if entry['status'] not in KEPT | {'rejected'}:
            raise ValueError('未知筛选状态')
        record = dict(source_candidate_id=entry['source_candidate_id'], factor_id=fid,
            status=entry['status'], statistical_pass=entry['statistical_pass'], reasons=entry['reasons'],
            source_path=str(path), source_sha256=sha(path),
            family_size=payload['inference']['family_size'],
            rank_ic_mean=payload['evaluation']['rank_ic_mean'],
            bonferroni_p_value=payload['evaluation']['bonferroni_p_value'])
        if record['source_candidate_id'] in records:
            raise ValueError('同一候选不能对应多个业务编号')
        records[record['source_candidate_id']] = record
        if entry['status'] in KEPT:
            if not entry['statistical_pass']:
                raise ValueError(f'{fid} 统计未通过，不能进入留库表')
            for field in IC_FIELDS:
                if not isinstance(payload['evaluation'].get(field), (int, float)) or not math.isfinite(payload['evaluation'][field]):
                    raise ValueError(f'{fid} 缺少完整IC指标')
            kept.append(dict(record=record, entry=entry, report=payload))
    # 配置已冻结而尚无最终筛选的候选也只占一条记录，不复制控制因子。
    for path in sorted(trials_root.glob('batch_*/config.json')):
        config = json.loads(path.read_text())
        for item in config['candidates']:
            cid = item['candidate_id']
            if cid not in records:
                records[cid] = dict(source_candidate_id=cid, factor_id=item['factor_id'], status='pending',
                    statistical_pass=None, reasons=['已登记，尚未完成筛选'], source_path=str(path),
                    source_sha256=sha(path), family_size=config['family_size'], rank_ic_mean=None, bonferroni_p_value=None)
    for path in sorted(trials_root.glob('preflight_failures/*/failure.json')):
        failure = json.loads(path.read_text())
        events = JsonlLedger(path.parent/'ledger').verify()
        if len(events) != 2 or events[-1].event_type.value != 'compile_failed':
            raise ValueError('编译失败账本不完整')
        cid = failure['candidate_id']
        if cid in records:
            raise ValueError('编译失败与已评价身份冲突')
        policy_path = trials_root / f"batch_{failure['batch_index']:03d}" / 'policy.json'
        family_size = json.loads(policy_path.read_text())['family_size']
        if not isinstance(family_size, int) or family_size < 1:
            raise ValueError('编译失败必须保留原批次检验族')
        records[cid] = dict(source_candidate_id=cid, factor_id=None, status='compile_failed',
            statistical_pass=False, reasons=[failure['reason']], source_path=str(path),
            source_sha256=sha(path), family_size=family_size, rank_ic_mean=None, bonferroni_p_value=None)
    return kept, list(records.values()), dict(review_path=str(review_path), review_sha256=sha(review_path))


DDL = """
CREATE TABLE IF NOT EXISTS public.factor_trial_records (
 market_id text NOT NULL CHECK(market_id='us_equity'), source_candidate_id text NOT NULL,
 factor_id text, status text NOT NULL CHECK(status IN ('retained','reserve','watch','rejected','pending','compile_failed')),
 statistical_pass boolean, reasons text[] NOT NULL, family_size integer,
 rank_ic_mean double precision, bonferroni_p_value double precision,
 source_path text NOT NULL, source_sha256 text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(market_id,source_candidate_id), UNIQUE(market_id,factor_id));
CREATE TABLE IF NOT EXISTS public.screened_factors (
 market_id text NOT NULL CHECK(market_id='us_equity'), factor_id text NOT NULL,
 source_candidate_id text NOT NULL, factor_name text NOT NULL,
 screening_status text NOT NULL CHECK(screening_status IN ('retained','reserve','watch')),
 statistical_pass boolean NOT NULL CHECK(statistical_pass),
 ic_mean double precision NOT NULL, rank_ic_mean double precision NOT NULL,
 ic_std double precision NOT NULL, rank_ic_std double precision NOT NULL,
 ic_ir double precision NOT NULL, rank_ic_ir double precision NOT NULL,
 ic_hac_t double precision NOT NULL, rank_ic_hac_t double precision NOT NULL,
 annualized_return double precision, max_drawdown double precision, sharpe double precision,
 reference_annualized_return double precision NOT NULL,
 unresolved_position_count integer NOT NULL, report jsonb NOT NULL, review jsonb NOT NULL,
 report_path text NOT NULL, report_sha256 text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(market_id,factor_id), UNIQUE(market_id,source_candidate_id));
COMMENT ON TABLE public.screened_factors IS '默认美股留库因子：统计通过，仍明确区分基底、观察与替补；不是全部闸门通过或实盘证明';
COMMENT ON TABLE public.factor_trial_records IS '每个尝试一条精简记录，包括淘汰、编译失败和未完成；完整证据保留在source_path引用的文件账本';
"""


def publish(profile: dict, trials_root: Path, receipt_root: Path, dsn: str = 'dbname=factor_miner_us connect_timeout=5') -> dict:
    """在单一事务中同步名录，先备份再移除已核验淘汰候选的旧详细投影。"""
    kept, records, identity = build_projection(profile, trials_root)
    receipt_root.mkdir(parents=True, exist_ok=False)
    retained_ids = [r['record']['factor_id'] for r in kept]
    rejected_ids = [r['factor_id'] for r in records if r['status'] == 'rejected']
    write_json(receipt_root/'plan.json', dict(**identity, retained_ids=retained_ids, rejected_ids=rejected_ids,
        trial_count=len(records), immutable_artifacts_action='只读，不删除正式研究证据'))
    with psycopg.connect(dsn) as conn, conn.transaction():
        if conn.execute('SELECT current_database()').fetchone()[0] != 'factor_miner_us':
            raise ValueError('拒绝修改其他数据库')
        conn.execute('SELECT pg_advisory_xact_lock(190907)')
        aliases = dict(conn.execute("SELECT source_candidate_id,factor_id FROM factor_miner_internal.factor_id_map WHERE market_id='us_equity'").fetchall())
        if any(r['factor_id'] is not None and aliases.get(r['source_candidate_id']) != r['factor_id'] for r in records):
            raise ValueError('稳定业务编号不一致')
        # 表名均为代码内常量。保存旧详细行，确保事务提交后仍可从文件恢复。
        backup = {}
        for table in ('public.factors', 'public.factor_metrics', 'public.visible_candidate_evaluations', 'factor_miner_internal.causal_reports'):
            backup[table] = [r[0] for r in conn.execute(f'SELECT to_jsonb(t) FROM {table} t WHERE factor_id=ANY(%s)', (rejected_ids,)).fetchall()]
        for table in ('public.screened_factors', 'public.factor_trial_records'):
            if conn.execute('SELECT to_regclass(%s)', (table,)).fetchone()[0] is not None:
                backup[table] = [r[0] for r in conn.execute(f"SELECT to_jsonb(t) FROM {table} t WHERE market_id='us_equity'").fetchall()]
        write_json(receipt_root/'database_rows_before.json', backup)
        conn.execute(DDL)
        for r in records:
            conn.execute("""INSERT INTO public.factor_trial_records
                (market_id,source_candidate_id,factor_id,status,statistical_pass,reasons,family_size,rank_ic_mean,bonferroni_p_value,source_path,source_sha256)
                VALUES('us_equity',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(market_id,source_candidate_id) DO UPDATE SET
                factor_id=EXCLUDED.factor_id,status=EXCLUDED.status,statistical_pass=EXCLUDED.statistical_pass,
                reasons=EXCLUDED.reasons,family_size=EXCLUDED.family_size,rank_ic_mean=EXCLUDED.rank_ic_mean,
                bonferroni_p_value=EXCLUDED.bonferroni_p_value,source_path=EXCLUDED.source_path,source_sha256=EXCLUDED.source_sha256,updated_at=now()""",
                tuple(r[k] for k in ('source_candidate_id','factor_id','status','statistical_pass','reasons','family_size','rank_ic_mean','bonferroni_p_value','source_path','source_sha256')))
        conn.execute("DELETE FROM public.screened_factors WHERE market_id='us_equity' AND NOT(factor_id=ANY(%s))", (retained_ids,))
        for item in kept:
            r, e, p = item['record'], item['entry'], item['report']
            unknown = len(p.get('unresolved_positions', []))
            metrics = p['reference_metrics']
            values = (r['factor_id'],r['source_candidate_id'],e['display_name'],e['status'],True,
                *(p['evaluation'][k] for k in IC_FIELDS),
                *(None if unknown else metrics[k] for k in ('annualized_return','max_drawdown','sharpe')),
                metrics['annualized_return'],unknown,Jsonb(p),Jsonb(e),r['source_path'],r['source_sha256'])
            columns = ['factor_id','source_candidate_id','factor_name','screening_status','statistical_pass',*IC_FIELDS,
                'annualized_return','max_drawdown','sharpe','reference_annualized_return','unresolved_position_count','report','review','report_path','report_sha256']
            conn.execute(f"INSERT INTO public.screened_factors(market_id,{','.join(columns)}) VALUES('us_equity',{','.join(['%s']*len(columns))}) ON CONFLICT(market_id,factor_id) DO UPDATE SET " + ','.join(f'{k}=EXCLUDED.{k}' for k in columns[1:]) + ',updated_at=now()', values)
        # 删除仅限清单里的淘汰编号；级联移除旧指标和旧可见评价，稳定ID映射永久保留。
        removed_reports = conn.execute('DELETE FROM factor_miner_internal.causal_reports WHERE factor_id=ANY(%s)', (rejected_ids,)).rowcount
        removed_factors = conn.execute("DELETE FROM public.factors WHERE market_id='us_equity' AND factor_id=ANY(%s)", (rejected_ids,)).rowcount
        actual = conn.execute("SELECT factor_id FROM public.screened_factors WHERE market_id='us_equity' ORDER BY factor_id").fetchall()
        if [r[0] for r in actual] != sorted(retained_ids):
            raise ValueError('留库表与冻结复核清单不一致')
        counts = dict(conn.execute("SELECT status,count(*) FROM public.factor_trial_records WHERE market_id='us_equity' GROUP BY status").fetchall())
        if sum(counts.values()) != len(records):
            raise ValueError('试验记录数量变化；不能缩小或覆盖未知历史')
        result = dict(status='published', screened_count=len(kept), trial_count=len(records), trial_status_counts=counts,
            removed_legacy_factor_rows=removed_factors, removed_legacy_report_rows=removed_reports,
            stable_id_map_preserved=True, backup_sha256=sha(receipt_root/'database_rows_before.json'), **identity)
    write_json(receipt_root/'receipt.json', result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--trials-root', type=Path, required=True)
    parser.add_argument('--receipt-root', type=Path, required=True)
    parser.add_argument('--dsn', default='dbname=factor_miner_us connect_timeout=5')
    args = parser.parse_args()
    profile = next(p for p in json.loads(args.profiles.read_text())['markets'] if p['market_id']=='us_equity')
    print(json.dumps(publish(profile,args.trials_root,args.receipt_root,args.dsn),ensure_ascii=False))


if __name__ == '__main__':
    main()
