"""下游原始宽表的身份、键并集和数值语义回归。"""
from datetime import date
import json

import polars as pl
import pytest

from factor_miner.feature_export import _year_panel, export_joint_features
from factor_miner.joint_library import adopt_library
from factor_miner.joint_study import digest
from tests.test_joint_library import make_study


def make_library(tmp_path):
    study = make_study(tmp_path)
    protocol = json.loads((study/'protocol.json').read_text())
    protocol['release'] = {'release_id': 'synthetic-v1', 'availability': 'after_close_t'}
    protocol['candidates'].append(dict(protocol['candidates'][0], factor_id='huan004', source_candidate_id='cand4'))
    for i, member in enumerate(protocol['candidates']):
        raw, spec = tmp_path/f'{i}.parquet', tmp_path/f'{i}.json'
        pl.DataFrame({'date': [date(2020, 1, 2), date(2021, 1, 4)],
                      'asset': ['A' if i == 0 else 'B']*2,
                      'raw_factor': [-3.0, float('nan') if i == 0 else 9.0],
                      'valid_for_factor_compute': [True, False]}).write_parquet(raw)
        spec.write_text('{}')
        member.update(raw_path=str(raw), spec_path=str(spec), original_direction='negative')
        for p in (raw, spec):
            protocol['input_sha256'][str(p.resolve())] = digest(p)
    (study/'protocol.json').write_text(json.dumps(protocol))
    roles = json.loads((study/'representatives.json').read_text())
    roles.append(dict(roles[0], factor_id='huan004', representative='huan004'))
    (study/'representatives.json').write_text(json.dumps(roles))
    (study/'completion.json').write_text(json.dumps({'artifacts': {
        p.name: digest(p) for p in study.iterdir() if p.name != 'completion.json'}}))
    root = tmp_path/'library'
    adopt_library(study, root)
    return root


def test_raw_export_preserves_union_direction_missingness_and_qualification(tmp_path):
    root = make_library(tmp_path)
    output = tmp_path/'features.parquet'
    result = export_joint_features(root, output)
    assert json.loads(json.dumps(result))['last_date'] == '2021-01-04'
    frame = pl.read_parquet(output)
    assert result['factor_count'] == 2
    assert frame.columns == ['date', 'order_book_id', 'factor_miner_huan002', 'factor_miner_huan004']
    assert frame.height == 4
    assert frame['factor_miner_huan002'].to_list() == [-3.0, None, None, None]
    assert frame['factor_miner_huan004'].to_list() == [None, -3.0, None, None]
    manifest = json.loads(output.with_suffix('.manifest.json').read_text())
    assert manifest['parquet_sha256'] == digest(output)
    assert manifest['formal_pass'] is None and manifest['sealed_oos'] is False
    assert manifest['contains_labels'] is False and manifest['standardized'] is False
    assert all(m['qualification'] == 'research_candidate_pending_confirmation' for m in manifest['members'])
    with pytest.raises(FileExistsError):
        export_joint_features(root, output)


def test_changed_source_cannot_be_exported(tmp_path):
    root = make_library(tmp_path)
    (tmp_path/'0.json').write_text('{"changed":true}')
    with pytest.raises(ValueError, match='已审核身份'):
        export_joint_features(root, tmp_path/'bad.parquet')
    assert not (tmp_path/'bad.parquet').exists()


def test_duplicate_keys_fail_before_join(tmp_path):
    root = make_library(tmp_path)
    member = json.loads((root/'library.json').read_text())['members'][0]
    frame = pl.read_parquet(member['raw_path'])
    pl.concat([frame, frame]).write_parquet(member['raw_path'])
    with pytest.raises(ValueError, match='重复主键'):
        _year_panel([dict(member, column='factor_miner_huan002')], 2020)


def test_cli_reports_success_after_publication(tmp_path):
    from typer.testing import CliRunner
    from factor_miner.cli import app
    root = make_library(tmp_path)
    result = CliRunner().invoke(app, ['export-joint-features', str(root), str(tmp_path/'cli.parquet')])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output.splitlines()[-1])['status'] == 'exported'
