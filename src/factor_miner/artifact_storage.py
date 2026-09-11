"""共享只读代码快照与未变化的上游面板；不修改或清理历史产物。"""
from pathlib import Path
import fcntl
import hashlib
import json
import os
import shutil
import tempfile


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def snapshot_code(root: Path, *, source: Path | None = None, directory: str = 'code',
                  write_identity: bool = True) -> dict:
    """按内容保存一份副本，各运行通过 code 目录链接读取；绝不链接工作代码。"""
    source = source or Path(__file__).parent
    identity = {str(p.relative_to(source)): digest_file(p) for p in sorted(source.rglob('*.py'))}
    if not identity:
        raise ValueError('代码快照不能为空')
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    store = Path(os.environ.get('FACTOR_MINER_CODE_STORE', str(root.parent / '.code_store'))).resolve()
    store.mkdir(parents=True, exist_ok=True)
    target = store / key
    with (store / '.writer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            if not target.exists():
                with tempfile.TemporaryDirectory(dir=store) as temporary:
                    stage = Path(temporary) / 'code'
                    stage.mkdir()
                    for name, expected in identity.items():
                        (stage / name).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source / name, stage / name)
                        if digest_file(stage / name) != expected:
                            raise ValueError('复制期间代码发生变化')
                        (stage / name).chmod(0o444)
                    stage.rename(target)
            actual = {str(p.relative_to(target)): digest_file(p) for p in target.rglob('*.py')}
            if actual != identity:
                raise ValueError('共享代码快照损坏；不能覆盖或静默重建')
            (root / directory).symlink_to(target, target_is_directory=True)
            if write_identity:
                with (root / 'code_identity.json').open('x') as stream:
                    json.dump(identity, stream, sort_keys=True)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return identity


def reference_panel(source: Path, target: Path) -> None:
    """引用已绑定哈希的只读上游；调用者继续在运行前后核验发布哈希。"""
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError('上游面板必须为文件')
    target.symlink_to(source)
