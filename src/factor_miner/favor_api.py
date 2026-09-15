"""Dashboard 与 CLI 共用的有限 API 生成任务，不绕过正式因子协议。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.favor_workflow import favor_generation_payload, load_plan, run_favor, submit_favor, verify_inputs
from factor_miner.ledger import atomic_write_bytes
from factor_miner.llm_online import (AgentRole, DEEPSEEK_MODEL, DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
    LLMExportAuthorization, build_deepseek_request)
from factor_miner.llm_privacy import registered_corporate_external_research_policy, scan_export_payload
from factor_miner.llm_provider import execute_recorded_call, UrllibDeepSeekTransport
from factor_miner.research_report import write_json


def public_request(root: Path, trial_id: str, max_tokens: int | None = None):
    payload = favor_generation_payload(root, trial_id, 'deepseek_api')
    # JSON Schema 描述含换行；仅规范空白，不移除零宽字符或放宽隐私扫描。
    def whitespace(value):
        if isinstance(value, str): return value.replace('\n',' ').replace('\r',' ').replace('\t',' ')
        if isinstance(value, dict): return {k:whitespace(v) for k,v in value.items()}
        if isinstance(value, list): return [whitespace(v) for v in value]
        return value
    payload = whitespace(payload)
    payload['information_class'] = 'public_research_hypothesis'
    request = build_deepseek_request(campaign_id='favor_'+payload['submission_identity']['plan_sha256'][:24],
        agent_role=AgentRole.EXPRESSION, slot_ids=(trial_id,),
        system_prompt='根据事前假设与测量合同生成一个因子，只返回 response_schema 要求的 JSON 对象。严禁修改假设、方向、字段或身份。',
        user_payload=payload)
    if max_tokens is not None:
        from hashlib import sha256
        body = dict(request.body, max_tokens=min(request.body['max_tokens'], max_tokens))
        request = type(request).model_validate(dict(request.model_dump(), body=body,
            request_sha256=sha256(canonical_json_bytes(body)).hexdigest()))
    return request


def public_policy(now, seconds):
    return registered_corporate_external_research_policy(provider='deepseek',
        allowed_endpoint=DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
        allowed_information_classes=('public_research_hypothesis',),
        forbidden_information_classes=('private_market_data','private_results','secrets'),
        allowed_models=(DEEPSEEK_MODEL,),maximum_authorized_campaigns=1,
        valid_from=now,valid_until=now+timedelta(seconds=seconds),
        approver_role='research_owner',approval_reference='dashboard_explicit_api_start')


def api_preflight(root: Path, market_id: str) -> dict:
    """只核对冻结计划及公开请求，不外发也不读取收益。"""
    plan = load_plan(root)
    if plan.market_id != market_id:
        raise ValueError('研究计划与当前市场不一致')
    if (root/'evaluation_started.json').exists() or (root/'summary.json').exists():
        raise ValueError('研究已经封存或完成，请查看结果')
    if (root/'api/launch.json').exists():
        raise ValueError('该研究已有 API 调用记录，不能重复发起或重置预算')
    from factor_miner.favor_workflow import file_sha
    code_identity=json.loads((root/'code_identity.json').read_text())
    if any(file_sha(Path(__file__).parent/name)!=identity for name,identity in code_identity.items()):
        raise ValueError('运行代码与冻结版本不一致，请使用原代码快照执行；不会先消耗 API')
    limits = plan.search_budget.get('resource_limits')
    if not isinstance(limits,dict) or not all(k in limits for k in ('model_responses','output_tokens','wall_seconds','artifact_bytes')):
        raise ValueError('计划缺少完整资源上限，请先登记有限研究计划')
    slots = [s.trial_id for s in plan.slots if not (root/'submissions'/s.trial_id).exists()]
    if not slots: raise ValueError('没有待生成的候选名额')
    # 外部导入的已有公式没有本接口计量，不能假设之前调用成本为零。
    if len(slots) != len(plan.slots):
        raise ValueError('本入口只启动尚未提交公式的完整计划，已有提交请使用原调用记录继续')
    if limits['model_responses'] < len(slots):
        raise ValueError('模型响应上限不足以覆盖候选名额')
    policy=public_policy(datetime.now(timezone.utc),limits['wall_seconds'])
    for trial in slots:
        scan_export_payload(public_request(root,trial).export_payload,policy)
    return dict(market_id=market_id,plan_sha256=sha256_json(plan.model_dump(mode='json')),
                slots=slots,resource_limits=limits,model=DEEPSEEK_MODEL)


def run_api_research(root: Path, market_id: str, plan_sha256: str, *, transport=None) -> dict:
    """每个计划只启动一次；不确定调用保留，停止请求在阶段边界生效。"""
    preview=api_preflight(root,market_id)
    if preview['plan_sha256'] != plan_sha256: raise ValueError('计划已变化，请重新预览')
    if not os.environ.get('DEEPSEEK_API_KEY'): raise ValueError('请先在设置中配置 DeepSeek API Key')
    folder=root/'api'
    folder.mkdir(exist_ok=False)
    now=datetime.now(timezone.utc)
    write_json(folder/'launch.json',dict(preview,requested_at=now.isoformat(),requested_by='research_owner'))
    limits=preview['resource_limits'];started=time.monotonic()
    status=dict(status='running',stage='准备',submitted=0,total=len(preview['slots']),
                attempted_calls=0,model_responses=0,output_tokens=0,failures=0,call_pending=False)
    def save(**updates):
        status.update(updates,updated_at=datetime.now(timezone.utc).isoformat(),elapsed_seconds=round(time.monotonic()-started,2))
        atomic_write_bytes(folder/'status.json',canonical_json_bytes(status))
    def boundary():
        if (folder/'stop.json').exists(): raise InterruptedError('已按请求停止；已消耗名额与调用记录保留')
        if time.monotonic()-started >= limits['wall_seconds']: raise InterruptedError('已达到研究时间上限')
        if sum(p.stat().st_size for p in root.rglob('*') if p.is_file() and not p.is_symlink() and 'code' not in p.parts and 'code_snapshot' not in p.parts) >= limits['artifact_bytes']:
            raise InterruptedError('已达到产物用量上限')
    import fcntl
    lock=(folder/'worker.lock').open('wb')
    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        save()
        verify_inputs(load_plan(root))
        policy=public_policy(now,limits['wall_seconds']);write_json(folder/'policy.json',policy.model_dump(mode='json'))
        for trial in preview['slots']:
            boundary()
            if status['model_responses'] >= limits['model_responses']: raise InterruptedError('已达到模型响应上限')
            remaining = None if limits['output_tokens'] is None else limits['output_tokens']-status['output_tokens']
            if remaining is not None and remaining <= 0: raise InterruptedError('已达到输出 Token 上限')
            request=public_request(root,trial,remaining)
            authorized=datetime.now(timezone.utc)
            auth=LLMExportAuthorization(authorization_id='dashboard_'+request.request_sha256[:24],
                campaign_id=request.campaign_id,request_sha256=request.request_sha256,corporate_policy_id=policy.policy_id,
                approver_role='research_owner',authorized_at=authorized,expires_at=policy.valid_until)
            write_json(folder/trial/'request.json',request.model_dump(mode='json'))
            write_json(folder/trial/'authorization.json',auth.model_dump(mode='json'))
            save(stage='API 生成公式',trial_id=trial,attempted_calls=status['attempted_calls']+1,call_pending=True)
            response=execute_recorded_call(prepared=request,authorization=auth,policy=policy,
                record_root=folder/trial,transport=transport or UrllibDeepSeekTransport(),now=authorized)
            output=response.record.usage.get('completion_tokens')
            if output is None:
                save(model_responses=status['model_responses']+1,output_tokens=None,call_pending=False)
                raise ValueError('供应商未返回输出 Token 用量，停止后续调用并保留响应')
            save(model_responses=status['model_responses']+1,output_tokens=status['output_tokens']+output,call_pending=False)
            payload=response.content_json
            expected=request.export_payload['submission_identity']
            if not isinstance(payload,dict) or any(payload.get(k)!=v for k,v in expected.items()):
                raise ValueError('API 响应未绑定当前计划与候选名额，已保留调用记录')
            receipt=submit_favor(root,payload)
            save(stage='公式校验',submitted=status['submitted']+1,
                 failures=status['failures']+int(receipt['status']!='compiled'))
            if response.record.finish_reason != 'stop': raise ValueError('API 响应未正常完成，停止后续调用')
        boundary()
        save(stage='构念、信号与成本诊断')
        run_favor(root,boundary_check=boundary)
        save(status='completed',stage='研究完成')
    except InterruptedError as error:
        save(status='stopped',stage='已停止',message=str(error))
    except Exception as error:
        # 供应商异常已经脱敏；不展示任意 HTTP 正文或凭据。
        from factor_miner.errors import FactorMinerError
        message=str(error) if isinstance(error,(ValueError,FactorMinerError)) else '执行失败，请检查本地任务记录与数据连接'
        if status['call_pending']:
            status.update(model_responses=None,output_tokens=None)
        save(status='failed',stage='需要处理',message=message)
    finally:
        lock.close()
    return status


def api_running(root: Path) -> bool:
    """活动锁随进程退出释放，避免把陈旧状态显示成仍在执行。"""
    import fcntl
    path=root/'api/worker.lock'
    if not path.exists():return False
    with path.open('rb') as stream:
        try:fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return True
        fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
    return False
