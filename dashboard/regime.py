"""共用状态分类控件，理论主张与实际发现分别展示。"""
from factor_miner.regime import RegimeRecord, record_fields


def render_regime(st, payload):
    if not payload:
        st.caption('预期适用状态：未登记 · 状态检验：未开展')
        return
    r = RegimeRecord.model_validate(payload)
    f = record_fields(r)
    st.markdown('#### 适用状态与失效条件')
    st.caption(f"{f['expected_regime_label']} · {f['regime_validation_status']} · {f['regime_origin']}")
    st.caption(f"{r.market_id} · {r.context_label} · {f['regime_period'] or '尚无检验区间'}")
    if r.results_seen:
        st.caption('历史收益区间已被观察；这是补充状态比较。'+('此前已查看过该因子的单项状态结果。' if r.previously_seen else ''))
    st.write('经济机制：'+r.mechanism)
    st.write('预期何时更强：'+r.spec.claim)
    st.write('可能减弱或失效：'+(r.spec.failure_condition or '尚未提出'))
    st.write('证伪办法：'+(r.spec.falsification or '尚未提出'))
    v = r.validation
    if v:
        st.write(v.interpretation)
        data = []
        for label,key,n in [('全期','unconditional',v.target_dates+v.control_dates),('目标状态','target',v.target_dates),('对照状态','control',v.control_dates),('目标减对照','difference',None)]:
            stat = getattr(v,key)
            if stat:
                data.append({'比较':label,'RankIC / 差值':stat.estimate,'有效期数':n,'95%区间':f'[{stat.ci95[0]:.4f}, {stat.ci95[1]:.4f}]','校正 p':stat.bonferroni_p})
        if data:
            st.dataframe(data,hide_index=True,width='stretch',column_config={
                'RankIC / 差值':st.column_config.NumberColumn(format='%.4f'),'校正 p':st.column_config.NumberColumn(format='%.4f')})
        total = v.target_dates+v.control_dates+v.unknown_dates
        st.caption(f'状态可用 {v.target_dates+v.control_dates}/{total} 期；未知 {v.unknown_dates} 期；检验族 {v.family_size}。置信区间为逐项区间，校正判断看校正 p。')
    with st.expander('状态测量与研究记录'):
        st.write('测量：'+r.spec.measurement_definition)
        st.write('时点：'+r.spec.availability+'；阈值：'+r.spec.threshold_rule)
        st.write('竞争解释：'+'；'.join(r.spec.competing_explanations))
        st.json(payload)
