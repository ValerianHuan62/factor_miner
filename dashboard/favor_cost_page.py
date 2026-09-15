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
    return [dict(r, 库内角色=members[r['编号']]['role'], 代表编号=members[r['编号']]['representative'],
                 审核状态='已审核 · 已采纳' if members[r['编号']]['role']=='研究代表' else '已审核 · 同类替补',
                 统计验证=r['状态']) for r in rows]


def render_cost_candidates(st, profile):
    if profile is None or profile.cost_review_path is None:
        st.info('当前市场尚未发布研究候选库。完成研究并采纳后，可在这里查看。')
        return
    try:
        rows=library_rows(profile.cost_review_path,profile.market_id,profile.joint_library_path)
    except (OSError,ValueError,KeyError) as error:
        st.error('研究库暂时无法读取，请检查当前市场的发布连接。')
        with st.expander('查看原因'): st.text(str(error))
        return
    adopted=bool(profile.joint_library_path)
    st.subheader(f'{profile.display_name} · 已审核因子' if adopted else f'{profile.display_name} · 研究候选')
    cols=st.columns(3)
    cols[0].metric('研究代表' if adopted else '研究候选',sum(r.get('库内角色')=='研究代表' for r in rows) if adopted else len(rows))
    cols[1].metric('同类替补',sum(r.get('库内角色')=='同类替补' for r in rows))
    cols[2].metric('已审核' if adopted else '待审核',len(rows))
    st.caption('研究代表用于后续策略与模型研究；同类替补保留备查。审核采纳和独立统计确认分别记录。' if adopted else '探索结果已入库，审核与独立统计确认分别记录。')
    left,right=st.columns([2,3])
    with left:
        scope=st.radio('成员范围',('研究代表','同类替补','全部'),horizontal=True,key=f'library_scope_{profile.market_id}') if adopted else '全部'
    with right:
        query=st.text_input('搜索因子',placeholder='编号、名称或测量',key=f'library_search_{profile.market_id}').strip().casefold()
    filtered=[r for r in rows if (scope=='全部' or r.get('库内角色')==scope) and
              (not query or query in (r['编号']+' '+r['名称']).casefold())]
    if not filtered:
        st.info('没有匹配的因子，请修改搜索或成员范围。');return
    visible=[dict(编号=r['编号'],名称=r['名称'],审核状态=r.get('审核状态','待审核'),
        IC=r['IC均值'],RankIC=r['RankIC均值'],参考年化=r['参考年化'],参考最大回撤=r['参考最大回撤']) for r in filtered]
    st.dataframe(visible,hide_index=True,width='stretch',column_config={
        '名称':st.column_config.TextColumn(width='large'),
        'IC':st.column_config.NumberColumn(format='%.6f'),
        'RankIC':st.column_config.NumberColumn(format='%.6f'),
        **{k:st.column_config.NumberColumn(format='percent') for k in ('参考年化','参考最大回撤')}})
    selected=st.selectbox('因子详情',[r['编号'] for r in filtered],
                          format_func=lambda v:next(r['编号']+' · '+r['名称'] for r in filtered if r['编号']==v),key=f'factor_detail_{profile.market_id}')
    row=next(r for r in filtered if r['编号']==selected)
    with st.container(border=True):
        st.markdown('#### '+row['名称'])
        st.caption(row.get('审核状态','待审核')+' · '+row.get('库内角色','研究候选'))
        st.code(row['公式'],language=None)
        metrics=st.columns(3)
        metrics[0].metric('参考 Sharpe',f"{row['参考Sharpe']:.2f}")
        metrics[1].metric('双边成本',f"{row['双边成本bps']:g} bps")
        metrics[2].metric('未确定持仓',row['未确定持仓'])
        if row.get('库内角色')=='同类替补':st.write('对应研究代表：'+row['代表编号'])
    with st.expander('统计验证与收益口径'):
        st.write('审核采纳：'+row.get('审核状态','未登记审核'))
        st.write('独立统计确认：'+row.get('统计验证',row['状态']))
        st.write('采纳记录已生效；该验证字段不要求重复人工确认。历史试验校正、独立机制与样本外验证按原协议记录。')
        st.write('参考收益按最后报价估值；持仓终值未确定时不代表已实现收益。')
        st.write('同类关系：'+row['同类关系'])
    st.download_button('下载当前因子清单', __import__('polars').DataFrame(visible).write_csv().encode('utf-8-sig'),
                       file_name=f'factor_miner_{profile.market_id}_catalog.csv',mime='text/csv')
