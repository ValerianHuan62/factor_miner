"""用户采纳的联合诊断代表库；成员角色不改变统计资格。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from factor_miner.joint_study import digest, read_json
from factor_miner.research_report import write_json


def validate_members(members: list[dict]) -> None:
    """所有替补必须直接对应同机制的有效代表。"""
    by_id = {row['factor_id']: row for row in members}
    if not members or len(by_id) != len(members):
        raise ValueError('成员为空或编号重复')
    if len({r['source_candidate_id'] for r in members}) != len(members):
        raise ValueError('原候选身份重复')
    for row in members:
        if (row['qualification'] != 'research_candidate_pending_confirmation'
                or row['role'] not in ('研究代表', '同类替补')
                or row['selection_uses_returns'] is not False):
            raise ValueError('成员角色或研究资格不符')
        rep = by_id.get(row['representative'])
        if not rep or rep['role'] != '研究代表' or rep['group'] != row['group']:
            raise ValueError('替补缺少同机制代表')
        if (row['role'] == '研究代表') != (row['factor_id'] == row['representative']):
            raise ValueError('代表映射不一致')


def load_library(root: Path) -> dict:
    """校验独立发布的成员清单与原累计库身份。"""
    receipt = read_json(root/'completion.json')
    if digest(root/'library.json') != receipt['library_sha256']:
        raise ValueError('代表库发布身份变化')
    value = read_json(root/'library.json')
    if value['version'] != 'joint-research-library-v1' or value['market_id'] != 'a_share':
        raise ValueError('代表库协议或市场不符')
    if value['formal_pass'] is not None or value['sealed_oos'] is not False:
        raise ValueError('不得将成员采纳升级为统计通过')
    for name, identity in value['snapshot_sha256'].items():
        if digest(root/'diagnostic'/name) != identity:
            raise ValueError('诊断快照身份变化')
    if digest(Path(value['collection'])/'collection.json') != value['collection_sha256']:
        raise ValueError('源累计候选库发生变化')
    validate_members(value['members'])
    return value


def adopt_library(study: Path, output: Path) -> dict:
    """显式采纳一次已完成诊断；不重选参数、不删除因子、不读取新收益。"""
    import shutil
    receipt = read_json(study/'completion.json')
    for name, identity in receipt['artifacts'].items():
        if digest(study/name) != identity:
            raise ValueError('联合诊断完成产物身份变化：'+name)
    protocol, summary = read_json(study/'protocol.json'), read_json(study/'summary.json')
    if (summary['status'] != 'completed_diagnostic' or protocol['formal_pass'] is not None
            or summary['formal_pass'] is not None or summary['sealed_oos'] is not False):
        raise ValueError('只接受已完成且边界明确的研究诊断')
    source = {x['factor_id']: x for x in protocol['candidates']}
    roles = read_json(study/'representatives.json')
    if {r['factor_id'] for r in roles} != set(source):
        raise ValueError('代表清单未覆盖完整候选库')
    members = [dict(source[r['factor_id']], **r) for r in roles]
    validate_members(members)
    collection = Path(protocol['config']['collection'])
    if digest(collection/'collection.json') != protocol['input_sha256'][str((collection/'collection.json').resolve())]:
        raise ValueError('候选源清单身份变化')
    output.mkdir(parents=True, exist_ok=False)
    (output/'diagnostic').mkdir()
    names = ('protocol.json', 'summary.json', 'representatives.json', 'completion.json', '研究结果.md', '研究结果.html')
    for name in names:
        shutil.copyfile(study/name, output/'diagnostic'/name)
    value = dict(version='joint-research-library-v1', market_id='a_share',
        adopted_at=datetime.now(timezone.utc).isoformat(), collection=str(collection.resolve()),
        collection_sha256=digest(collection/'collection.json'), source_study=str(study.resolve()),
        snapshot_sha256={name:digest(output/'diagnostic'/name) for name in names},
        formal_pass=None, sealed_oos=False, members=members)
    write_json(output/'library.json', value)
    write_json(output/'completion.json', {'library_sha256':digest(output/'library.json')})
    return {'status':'adopted', 'representatives':sum(r['role']=='研究代表' for r in members),
            'reserves':sum(r['role']=='同类替补' for r in members), 'root':str(output)}


def active_members(root: Path, include_reserves: bool = False) -> list[dict]:
    """下游按显式代表清单加载原矩阵，不复制矩阵或恢复替补。"""
    return [r for r in load_library(root)['members'] if include_reserves or r['role']=='研究代表']
