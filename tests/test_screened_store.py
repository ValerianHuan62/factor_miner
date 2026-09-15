"""统计未通过、身份错配、缺失指标和未知收益不能混入留库指标。"""
import hashlib
import json
from pathlib import Path
import pytest
from dashboard.screened_store import build_projection, metric_rows, IC_FIELDS


def fixture(tmp_path):
    reports = tmp_path/'reports'; reports.mkdir()
    payload = dict(source_candidate_id='cand_one',market_id='us_equity',direction='negative',scope='探索性诊断',
        inference={'family_size':45}, evaluation={**{k:.1 for k in IC_FIELDS},'bonferroni_p_value':.01,'valid_dates':100,'median_coverage':.9},
        reference_metrics=dict(annualized_return=.2,max_drawdown=.1,sharpe=1.,information_ratio=.5),unresolved_positions=[{'security_id':'x'}])
    path=reports/'huan001.json';path.write_text(json.dumps(payload))
    entry=dict(source_candidate_id='cand_one',status='watch',statistical_pass=True,reasons=['增量待验证'],
        display_name='观察候选',label='观察池',report_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    review=tmp_path/'review.json';review.write_text(json.dumps(dict(market_id='us_equity',schema_version='library-review-v1',by_factor={'huan001':entry})))
    return dict(market_id='us_equity',library_review_path=str(review),backtest_roots=[str(reports)]),payload,entry


def test_kept_and_rejected_use_distinct_storage(tmp_path):
    profile,payload,entry=fixture(tmp_path)
    kept,records,_=build_projection(profile,tmp_path)
    assert len(kept)==len(records)==1
    assert 'report' not in records[0] and 'economic' not in records[0]
    entry['status']='rejected'
    path=Path(profile['library_review_path']);r=json.loads(path.read_text());r['by_factor']['huan001']=entry;path.write_text(json.dumps(r))
    kept,records,_=build_projection(profile,tmp_path)
    assert kept==[] and len(records)==1


def test_statistical_failure_cannot_be_marked_kept(tmp_path):
    profile,_,_=fixture(tmp_path)
    path=Path(profile['library_review_path']);r=json.loads(path.read_text());r['by_factor']['huan001']['statistical_pass']=False;path.write_text(json.dumps(r))
    with pytest.raises(ValueError,match='统计未通过'):build_projection(profile,tmp_path)


def test_mutated_report_rejected(tmp_path):
    profile,_,_=fixture(tmp_path)
    path=Path(profile['backtest_roots'][0])/'huan001.json';path.write_text(path.read_text()+' ')
    with pytest.raises(ValueError,match='内容变化'):build_projection(profile,tmp_path)


def test_reader_does_not_invent_actual_returns(tmp_path):
    _,payload,entry=fixture(tmp_path)
    class Connection:
        def execute(self,*args):return self
        def fetchall(self):return [('huan001',payload,entry,None)]
    row=metric_rows(Connection())[0]
    assert row['rank_ic_mean']==.1 and row['annualized_return'] is None and row['has_portfolio'] is False
