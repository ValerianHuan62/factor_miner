"""本机 API 配置与后台 CLI 连接；凭据不进入任务参数和日志。"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from dashboard.market_profiles import profile_by_id


def settings_path() -> Path:
    return Path(__file__).resolve().parents[1]/'configs/dashboard_api.local.json'


def read_settings() -> dict:
    path=settings_path()
    return json.loads(path.read_text()) if path.exists() else {}


def save_settings(api_key: str, templates: dict[str,str]) -> None:
    """密钥只保存到本机私有配置；空输入保留已配置值。"""
    old=read_settings()
    value=dict(api_key=api_key.strip() or old.get('api_key',''),templates=templates)
    path=settings_path();path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.api-config-')
    try:
        with os.fdopen(fd,'w') as stream: json.dump(value,stream,ensure_ascii=False,indent=2)
        os.chmod(name,0o600)
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def key_available() -> bool:
    return bool(os.environ.get('DEEPSEEK_API_KEY') or read_settings().get('api_key'))


def run_roots(profile) -> tuple[Path,...]:
    return tuple(dict.fromkeys(p for p in (profile.research_root,profile.artifact_root) if p))


def validate_run_path(root: Path, market_id: str) -> None:
    profile=profile_by_id(market_id)
    if profile is None or not any(root.resolve().is_relative_to((p/'favor').resolve()) for p in run_roots(profile)):
        raise ValueError('研究目录不在当前市场的已配置范围内')


def start_api(root: Path, market_id: str, identity: str) -> subprocess.Popen:
    from factor_miner.favor_api import api_preflight
    validate_run_path(root,market_id)
    preview=api_preflight(root,market_id)
    if preview['plan_sha256'] != identity: raise ValueError('研究计划已变化，请重新预览')
    key=os.environ.get('DEEPSEEK_API_KEY') or read_settings().get('api_key')
    if not key: raise ValueError('请先在设置中配置 DeepSeek API Key')
    env=dict(os.environ,DEEPSEEK_API_KEY=key)
    # 原子单次派发，Streamlit 重跑或重复点击都不能多发一次 API。
    with (root/'api-dispatch.json').open('x') as stream:
        json.dump(dict(plan_sha256=identity,market_id=market_id),stream)
    try:
        with (root/'api-worker.log').open('ab') as log:
            return subprocess.Popen([sys.executable,'-m','factor_miner.cli','run-favor-api',str(root),
                '--market-id',market_id,'--plan-sha256',identity],cwd=Path(__file__).resolve().parents[1],
                env=env,stdout=log,stderr=log,start_new_session=True)
    except OSError:
        (root/'api-dispatch.json').unlink()
        raise


def register_plan(payload: dict, market_id: str) -> Path:
    """页面只提交明确计划，由正式 CLI 预检、登记与冻结。"""
    from uuid import uuid4
    from factor_miner.favor_schema import parse_favor_plan
    plan=parse_favor_plan(payload)
    if plan.market_id!=market_id or plan.version!='favor-exploration-v1' or plan.run_kind!='research':
        raise ValueError('新增入口只接受当前市场的正式探索计划')
    profile=profile_by_id(market_id)
    if profile is None:raise ValueError('市场尚未配置')
    base=profile.research_root or profile.artifact_root
    root=base/'favor'/('api_'+uuid4().hex[:12])
    drafts=base/'api_drafts';drafts.mkdir(parents=True,exist_ok=True)
    path=drafts/(root.name+'.json')
    path.write_text(plan.model_dump_json(indent=2))
    result=subprocess.run([sys.executable,'-m','factor_miner.cli','register-favor',str(path),str(root)],
        cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
    if result.returncode:
        raise ValueError('登记未通过：'+(result.stdout+result.stderr)[-2000:])
    return root
