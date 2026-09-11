"""带稳定编号的研究候选库；实际与参考收益分列。"""
import json
from pathlib import Path
from factor_miner.favor_cost_review import load_cost_review
from factor_miner.favor_workflow import file_sha


def candidate_rows(root: Path, market_id: str) -> list[dict]:
    """累计清单保留全部已发布批次，不能以新批次遮蔽旧候选。"""
    collection = root/'collection.json'
    if collection.exists():
        manifest = json.loads(collection.read_text())
        if manifest['market_id'] != market_id or not manifest['reviews']:
            raise ValueError('累计研究库市场或批次清单不符')
        rows = []; seen = set()
        for item in manifest['reviews']:
            path = Path(item['root'])
            if (path/'collection.json').exists(): raise ValueError('累计清单不允许嵌套')
            for name in ('summary','publication'):
                if file_sha(path/(name+'.json')) != item[name+'_sha256']:
                    raise ValueError('累计研究库发布身份变化')
            for row in candidate_rows(path, market_id):
                if row['编号'] in seen: raise ValueError('累计研究库候选编号重复')
                seen.add(row['编号']); rows.append(row)
        return sorted(rows, key=lambda row:int(row['编号'][4:]))
    summary = load_cost_review(root)
    publication = json.loads((root/'publication.json').read_text())
    if summary['market_id'] != market_id or publication['summary_sha256'] != file_sha(root/'summary.json'):
        raise ValueError('研究候选库市场或发布身份不符')
    rows=[]
    for r in summary['results']:
        if not r['admission']['eligible']: continue
        rows.append({'编号':publication['aliases'][r['trial_id']], '名称':r['name'],
            '原候选':r['trial_id'],'公式':publication['formulas'][r['trial_id']], '状态':'待正式确认', 'IC均值':r['discovery']['ic_mean'],
            'RankIC均值':r['discovery']['rank_ic_mean'],'双边成本bps':r['round_trip_cost_bps'],
            '实际年化':(r['metrics']['actual'] or {}).get('annualized_return'),
            '参考年化':r['metrics']['reference']['annualized_return'],
            '参考最大回撤':r['metrics']['reference']['max_drawdown'],
            '参考Sharpe':r['metrics']['reference']['sharpe'],
            '参考信息比':r['metrics']['reference']['information_ratio'],
            '未确定持仓':r['unresolved_positions'],
            '同类关系':'；'.join(f"{x['trial_id']}：{x['p95_abs_spearman']:.3f}" for x in r['related_candidates']) or '未发现已登记的同类关系；非全库增量确认'})
    return rows


def library_rows(root: Path, market_id: str, library: Path | None = None) -> list[dict]:
    """代表库是成员覆盖层，基础指标继续使用原发布。"""
    rows = candidate_rows(root, market_id)
    if library is None:
        return rows
    from factor_miner.joint_library import load_library
    value = load_library(library)
    if market_id != value['market_id'] or root.resolve() != Path(value['collection']).resolve():
        raise ValueError('代表库与当前市场或累计库不符')
    members = {r['factor_id']:r for r in value['members']}
    if set(members) != {r['编号'] for r in rows}:
        raise ValueError('代表库覆盖范围与原候选库不符')
    return [dict(r, 库内角色=members[r['编号']]['role'], 代表编号=members[r['编号']]['representative']) for r in rows]


def render_cost_candidates(st, profile):
    if profile is None or profile.cost_review_path is None: return
    try: rows=library_rows(profile.cost_review_path,profile.market_id,getattr(profile,'joint_library_path',None))
    except (OSError,ValueError,KeyError) as error:
        st.error('研究候选库读取失败：'+str(error));return
    st.subheader('研究候选库 · 待正式确认')
    st.caption('按原方向、基础测量检验和双边万14参考净收益筛选。历史统计校正未知，尚未完成独立机制与正式确认；实际收益为空时，参考估值不代表已实现收益。')
    if getattr(profile,'joint_library_path',None):
        st.caption(f"已采纳精简研究池：{sum(r['库内角色']=='研究代表' for r in rows)} 个代表，{sum(r['库内角色']=='同类替补' for r in rows)} 个替补。")
        scope=st.radio('查看范围',('研究代表','同类替补','全部'),horizontal=True,key='joint_library_scope')
        rows=[r for r in rows if scope=='全部' or r['库内角色']==scope]
    visible=('编号','原候选','名称','IC均值','RankIC均值','参考年化','参考最大回撤','状态')
    st.dataframe([{k:r[k] for k in visible} for r in rows],hide_index=True,width='stretch',column_config={
        '名称':st.column_config.TextColumn(width='medium'),
        'IC均值':st.column_config.NumberColumn('IC 均值（ic_mean）',format='%.6f'),
        'RankIC均值':st.column_config.NumberColumn(format='%.6f'),
        **{k:st.column_config.NumberColumn(format='percent') for k in ('实际年化','参考年化','参考最大回撤')}})
    with st.expander('公式、实际收益与同类关系'):
        st.dataframe(rows,hide_index=True,width='stretch')
