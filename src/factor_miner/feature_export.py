"""将已采纳研究代表发布为下游可读取的原始因子宽表。"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile

import polars as pl

from factor_miner.joint_library import load_library
from factor_miner.joint_study import digest, read_json
from factor_miner.research_report import write_json


def _year_panel(members: list[dict], year: int) -> pl.DataFrame:
    """按键并集连接，不因其他因子缺失而删除证券或日期。"""
    panel = None
    for member in members:
        frame = pl.scan_parquet(member['raw_path']).filter(pl.col('date').dt.year() == year).select(
            'date', 'asset', 'raw_factor', 'valid_for_factor_compute',
        ).collect(engine='streaming')
        if frame.select(pl.any_horizontal(pl.col('date').is_null(), pl.col('asset').is_null(),
                                          pl.col('valid_for_factor_compute').is_null()).any()).item():
            raise ValueError('源矩阵主键或计算资格缺失：' + member['factor_id'])
        if frame.select(pl.struct('date', 'asset').is_duplicated().any()).item():
            raise ValueError('源矩阵存在重复主键：' + member['factor_id'])
        if frame.select(pl.col('raw_factor').is_infinite().any()).item():
            raise ValueError('源矩阵含无穷值：' + member['factor_id'])
        frame = frame.select('date', pl.col('asset').alias('order_book_id'),
            pl.when(pl.col('valid_for_factor_compute')).then(pl.col('raw_factor').fill_nan(None))
            .otherwise(None).alias(member['column']))
        panel = frame if panel is None else panel.join(frame, on=['date', 'order_book_id'],
                                                       how='full', coalesce=True, validate='1:1')
    if panel is None:
        raise ValueError('没有已采纳的研究代表')
    return panel.select('date', 'order_book_id', *(m['column'] for m in members)).sort('date', 'order_book_id')


def export_joint_features(library_root: Path, output: Path) -> dict:
    """校验来源后按年导出；显式输出路径、不可覆盖，不生成标签或标准化值。"""
    library_root, output = library_root.resolve(), output.absolute()
    if output.suffix != '.parquet':
        raise ValueError('输出必须为 .parquet 文件')
    manifest = output.with_suffix('.manifest.json')
    if output.exists() or output.is_symlink() or manifest.exists() or manifest.is_symlink():
        raise FileExistsError('输出已存在，请使用新的发布文件名')
    library = load_library(library_root)
    protocol = read_json(library_root/'diagnostic/protocol.json')
    if protocol['release'].get('availability') != 'after_close_t':
        raise ValueError('本出口要求显式的收盘后日频可得时间')
    members = [dict(row, column='factor_miner_' + row['factor_id'])
               for row in library['members'] if row['role'] == '研究代表']
    if not members:
        raise ValueError('没有已采纳的研究代表')
    inputs = {str(library_root/'library.json'): digest(library_root/'library.json')}
    years = set()
    for member in members:
        for key in ('raw_path', 'spec_path'):
            source = Path(member[key]).resolve()
            expected = protocol['input_sha256'].get(str(source))
            if expected is None or digest(source) != expected:
                raise ValueError('源因子与已审核身份不一致：' + str(source))
            inputs[str(source)] = expected
        schema = pl.scan_parquet(member['raw_path']).collect_schema()
        if (schema.get('date') != pl.Date or schema.get('asset') != pl.String
                or schema.get('raw_factor') != pl.Float64
                or schema.get('valid_for_factor_compute') != pl.Boolean):
            raise ValueError('原始因子矩阵结构不符合出口合同')
        years.update(pl.scan_parquet(member['raw_path']).select(
            pl.col('date').dt.year().unique()).collect()['date'].to_list())
    if not years or None in years:
        raise ValueError('源矩阵日期为空或缺失')
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_suffix('.export.lock')
    with lock.open('x') as stream:
        stream.write(str(os.getpid()))
    try:
        with tempfile.TemporaryDirectory(prefix='.factor-miner-export-', dir=output.parent) as folder:
            temp = Path(folder)
            parts, summaries = [], []
            for year in sorted(years):
                frame = _year_panel(members, year)
                part = temp/f'{year}.parquet'
                frame.write_parquet(part, compression='snappy')
                if not pl.read_parquet(part).equals(frame):
                    raise ValueError('年度宽表写入回读不一致')
                summaries.append(dict(year=year, rows=frame.height,
                    null_counts={m['column']:frame[m['column']].null_count() for m in members}))
                parts.append(part)
                print(f'Factor Miner 宽表导出：{year} 年，{frame.height} 行，{len(members)} 个因子', flush=True)
                del frame
            staged = temp/output.name
            pl.concat([pl.scan_parquet(p) for p in parts]).sink_parquet(staged, compression='snappy')
            scan = pl.scan_parquet(staged)
            stats = scan.select(pl.len().alias('rows'), pl.col('date').min().alias('first_date'),
                pl.col('date').max().alias('last_date'), pl.col('order_book_id').n_unique().alias('securities')).collect().row(0, named=True)
            if stats['rows'] != sum(s['rows'] for s in summaries):
                raise ValueError('最终宽表行数不一致')
            for key in ('first_date', 'last_date'):
                stats[key] = stats[key].isoformat()
            # 导出期间的源数据变化不能成为一个已完成发布。
            load_library(library_root)
            for path, identity in inputs.items():
                if digest(Path(path)) != identity:
                    raise ValueError('导出期间源输入发生变化：' + path)
            value = dict(version='factor-miner-raw-feature-export-v1', producer='factor_miner',
                created_at=datetime.now(timezone.utc).isoformat(), output=str(output),
                parquet_sha256=digest(staged), parquet_bytes=staged.stat().st_size,
                library_root=str(library_root), input_sha256=inputs, release=protocol['release'],
                exporter_sha256=digest(Path(__file__)), columns=['date','order_book_id',*(m['column'] for m in members)],
                keys=['date','order_book_id'], factor_count=len(members), members=members,
                value_semantics='原始因子；不翻转方向、不标准化、不填零；计算无效与 NaN 保留为 null；按日期证券键并集连接',
                availability='after_close_t', earliest_execution='next_market_session',
                contains_labels=False, standardized=False, formal_pass=library['formal_pass'],
                sealed_oos=library['sealed_oos'], review_status='已采纳研究代表；原资格仍待正式确认',
                selection_history=protocol['config'], partitions=summaries, **stats)
            staged_manifest = temp/manifest.name
            write_json(staged_manifest, value)
            # 同目录硬链接是不可覆盖的发布操作；清单是完整发布的完成标记。
            os.link(staged, output)
            os.link(staged_manifest, manifest)
            return dict(status='exported', producer='factor_miner', output=str(output),
                        manifest=str(manifest), factor_count=len(members), **stats)
    finally:
        lock.unlink()
