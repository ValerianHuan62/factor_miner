"""状态主张与只读展示合同；独立于原始假设、筛选结论及交易触发。"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator
from factor_miner.canonical import sha256_json

REGIME_PROMPT = (
    '在生成表达式之前，说明机制是否预测可观察的状态差异。选择一个主要状态，说明作用渠道、'
    '信号时可获得的代理、预期增强方向、对照状态及可能失效情形，写明证伪判据与竞争解释。'
    '区分更强、只有该状态有效、符号反转；首版只执行更强比较。没有依据时明确填写无条件主张及原因，'
    '不能解释成全状态有效。仅用允许的数据，不依据收益挑状态；状态冻结后公式生成器不得修改。'
)
STATUS_LABELS = {'untested': '待检验', 'awaiting_data': '待数据', 'insufficient_sample': '样本不足',
                 'no_support': '证据不足', 'supported': '支持预期增强', 'not_evaluated': '本次未评价'}
STATE_LABELS = {'liquidity': '流动性压力', 'volatility': '波动', 'trend': '趋势持续性', 'activity': '交易活跃度'}


class Frozen(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)


class RegimeHypothesisSpec(Frozen):
    """不嵌入旧 HypothesisSpec，避免改变历史序列化。"""
    version: Literal['regime-hypothesis-v1'] = 'regime-hypothesis-v1'
    has_claim: bool
    claim: str = Field(min_length=1)
    dimension: str = Field(min_length=1)
    direction: Literal['high', 'low', 'none']
    effect: Literal['stronger', 'exclusive', 'sign_reversal', 'none']
    scope: Literal['market_time', 'asset_cross_section', 'none']
    measurement_ref: str
    measurement_definition: str
    required_fields: tuple[str, ...]
    availability: str
    lag_sessions: int = Field(ge=0)
    threshold_rule: str
    missing_rule: Literal['unknown'] = 'unknown'
    data_available: bool
    unavailable_reason: str
    control_state: str
    failure_condition: str
    falsification: str
    competing_explanations: tuple[str, ...]

    @model_validator(mode='after')
    def coherent(self):
        if self.has_claim:
            values = (self.measurement_ref, self.measurement_definition, self.availability,
                      self.threshold_rule, self.control_state, self.failure_condition, self.falsification)
            if not all(values) or not self.required_fields or not self.competing_explanations:
                raise ValueError('状态主张必须有测量、时点、对照、失效及证伪定义')
            if self.direction == 'none' or self.effect == 'none' or self.scope == 'none':
                raise ValueError('有状态主张时必须明确方向、效应和层级')
            if not self.data_available and not self.unavailable_reason:
                raise ValueError('缺数据必须说明原因')
        elif self.effect != 'none' or self.direction != 'none' or self.scope != 'none' or self.dimension != 'none':
            raise ValueError('无条件主张不能标为全状态有效')
        return self

    @property
    def identity(self):
        return sha256_json(self.model_dump(mode='json'))

    @property
    def label(self):
        return ('高' if self.direction == 'high' else '低') + STATE_LABELS.get(self.dimension, self.dimension) if self.has_claim else '尚无明确条件假设'


class Statistic(Frozen):
    estimate: FiniteFloat
    standard_error: FiniteFloat = Field(ge=0)
    hac_t: FiniteFloat
    raw_p: FiniteFloat = Field(ge=0, le=1)
    bonferroni_p: FiniteFloat = Field(ge=0, le=1)
    ci95: tuple[FiniteFloat, FiniteFloat]

    @model_validator(mode='after')
    def bounds(self):
        if self.ci95[0] > self.ci95[1] or self.bonferroni_p < self.raw_p:
            raise ValueError('统计区间或校正 p 值无效')
        return self


class RegimeValidation(Frozen):
    run_id: str
    spec_sha256: str
    context_id: str
    status: Literal['insufficient_sample', 'no_support', 'supported', 'not_evaluated']
    start: str
    end: str
    test_consumed: bool
    sealed_oos: bool
    target_dates: int = Field(ge=0)
    control_dates: int = Field(ge=0)
    unknown_dates: int = Field(ge=0)
    target: Statistic | None = None
    control: Statistic | None = None
    difference: Statistic | None = None
    unconditional: Statistic | None = None
    family_size: int = Field(ge=1)
    alpha: FiniteFloat = Field(gt=0, lt=1)
    minimum_effect: FiniteFloat | None = None
    protocol_sha256: str
    source_path: str
    source_sha256: str
    interpretation: str
    supersedes: str | None = None

    @model_validator(mode='after')
    def valid_result(self):
        if self.start > self.end or (self.sealed_oos and self.test_consumed):
            raise ValueError('检验区间或样本外性质冲突')
        if self.status in {'no_support', 'supported'}:
            if not all([self.target, self.control, self.difference, self.unconditional]) or min(self.target_dates, self.control_dates) == 0:
                raise ValueError('已评价结果必须有完整统计及两组样本')
        if self.status == 'supported':
            if self.minimum_effect is None or self.difference.estimate <= self.minimum_effect or self.difference.bonferroni_p > self.alpha:
                raise ValueError('通过标签缺少预登记效应或校正支持')
        return self


class RegimeRecord(Frozen):
    market_id: Literal['us_equity', 'a_share']
    source_candidate_id: str
    factor_id: str
    hypothesis_sha256: str
    mechanism: str
    origin: Literal['historical_addendum', 'before_expression']
    registered_at: datetime
    results_seen: bool
    previously_seen: bool
    context_id: str
    context_label: str
    spec: RegimeHypothesisSpec
    validation: RegimeValidation | None = None

    @model_validator(mode='after')
    def matched(self):
        if self.registered_at.tzinfo is None:
            raise ValueError('登记时间必须含时区')
        if self.origin == 'before_expression' and self.results_seen:
            raise ValueError('已见结果不能登记为事前假设')
        if self.validation and (self.validation.spec_sha256 != self.spec.identity or self.validation.context_id != self.context_id):
            raise ValueError('状态定义或评价上下文不匹配')
        if self.validation and self.validation.status == 'supported' and (self.spec.effect != 'stronger' or self.spec.scope != 'market_time'):
            raise ValueError('首版只支持市场时间状态的增强比较')
        return self


class RegimeManifest(Frozen):
    version: Literal['regime-manifest-v1'] = 'regime-manifest-v1'
    market_id: Literal['us_equity', 'a_share']
    context_id: str
    records: tuple[RegimeRecord, ...]
    source_files: dict[str, str]

    @model_validator(mode='after')
    def unique(self):
        sources = [r.source_candidate_id for r in self.records]
        aliases = [r.factor_id for r in self.records]
        if len(set(sources)) != len(sources) or len(set(aliases)) != len(aliases):
            raise ValueError('展示清单候选或编号重复')
        if any(r.market_id != self.market_id or r.context_id != self.context_id for r in self.records):
            raise ValueError('展示清单市场或上下文不符')
        return self


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(path: Path, market_id: str) -> RegimeManifest:
    manifest = RegimeManifest.model_validate_json(path.read_text())
    if manifest.market_id != market_id:
        raise ValueError('状态展示清单市场不符')
    for source, expected in manifest.source_files.items():
        if file_sha(Path(source)) != expected:
            raise ValueError('状态展示来源内容变化')
    return manifest


def save_manifest(manifest: RegimeManifest, path: Path) -> None:
    """追加发布，不覆盖旧清单；同内容重试幂等。"""
    payload = manifest.model_dump_json(indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text() != payload:
            raise ValueError('清单路径已有不同内容，请使用新发布路径')
        return
    with path.open('x') as stream:
        stream.write(payload)


def record_fields(record: RegimeRecord | None) -> dict:
    """文件与 PG 共用同一中文语义。"""
    if record is None:
        return dict(expected_regime_label='未登记', regime_validation_status='未开展', regime_dimension='未登记',
                    regime_direction='未登记', failure_condition_summary='', regime_origin='', regime_period='', regime_context='')
    s, v = record.spec, record.validation
    status = STATUS_LABELS[v.status] if v else ('未开展' if not s.has_claim else '待数据' if not s.data_available else '待检验')
    return dict(expected_regime_label=s.label, regime_validation_status=status,
                regime_dimension=STATE_LABELS.get(s.dimension, s.dimension), regime_direction={'high':'高','low':'低','none':'无'}[s.direction],
                failure_condition_summary=s.failure_condition, regime_origin='历史补充假设' if record.origin == 'historical_addendum' else '公式生成前登记',
                regime_period=f'{v.start} 至 {v.end} · '+('历史复核' if v.test_consumed else '密封样本外' if v.sealed_oos else '非密封检验') if v else '',
                regime_context=record.context_label)


def attach_records(rows: list[dict], manifest: RegimeManifest) -> list[dict]:
    by_source = {r.source_candidate_id:r for r in manifest.records}
    by_alias = {r.factor_id:r for r in manifest.records}
    result = []
    for row in rows:
        if row.get('market_id') != manifest.market_id:
            raise ValueError('状态叠加的报告市场不符')
        source = row.get('source_candidate_id')
        record = by_source.get(source)
        if row.get('factor_id') in by_alias and record != by_alias[row['factor_id']]:
            raise ValueError('状态分类的因子身份不匹配')
        if record and row.get('factor_id') != record.factor_id:
            raise ValueError('状态分类的稳定编号不匹配')
        result.append({**row, **record_fields(record), 'regime_record': record.model_dump(mode='json') if record else None})
    return result


def import_state_batch(root: Path, output: Path) -> RegimeManifest:
    """导入已经完成的冻结比较；不读取收益面板、不重跑研究。"""
    if (root/'correction_notice.json').exists():
        raise ValueError('该实验已标记纠错替代，请显式选择纠正后的发布')
    completion = json.loads((root/'completion.json').read_text())
    if completion['status'] != 'completed' or not completion['inputs_unchanged']:
        raise ValueError('状态实验未完整完成')
    for name, digest in completion['result_sha256'].items():
        if file_sha(root/name) != digest:
            raise ValueError(f'状态实验产物变化：{name}')
    protocol = json.loads((root/'protocol.json').read_text())
    if protocol['version'] != 'state-hypothesis-batch-v1' or any(c['active_value'] != 1 for c in protocol['factors']):
        raise ValueError('此导入器仅支持冻结的高状态增强比较')
    factors = json.loads((root/'factor_results.json').read_text())
    cards = json.loads((root/'hypothesis_cards.json').read_text())
    if cards != protocol['factors']:
        raise ValueError('假设卡片与冻结协议不符')
    if {f['factor_id'] for f in factors} != {c['factor_id'] for c in cards} or len(factors) != len(cards):
        raise ValueError('实验结果候选不完整或重复')
    context = 'historical_5d_' + sha256_json({k:protocol[k] for k in ['panel_path','start','boundary','end','input_sha256']})[:16]
    definitions = {
        'liquidity': ('报价相对价差截面中位数的20日均值', ('quote_bid_raw','quote_ask_raw','valid_closing_quote')),
        'volatility': ('市场等权日收益过去20日标准差', ('close',)),
        'trend': ('市场过去20日收益之和绝对值除以绝对收益之和', ('close',)),
        'activity': ('个股5日/60日平均成交额之比的截面中位数', ('close','volume')),
    }
    records = []
    for card in cards:
        result = next(f for f in factors if f['factor_id'] == card['factor_id'])
        stage = result['stages']['historical_review']
        state = card['state_id']
        spec = RegimeHypothesisSpec(has_claim=True, claim=card['expected_state'], dimension=state,
            direction='high' if card['active_value'] == 1 else 'low', effect='stronger', scope='market_time',
            measurement_ref=protocol['version']+':'+state, measurement_definition=definitions[state][0],
            required_fields=definitions[state][1], availability='收盘数据计算后再滞后一市场日', lag_sessions=1,
            threshold_rule=protocol['state_policy'], data_available=True, unavailable_reason='',
            control_state='主要状态的另一组；缺失为未知', failure_condition=card['failure_condition'],
            falsification=card['falsification_rule'], competing_explanations=card['original_hypothesis']['competing_explanations'])
        status = 'no_support' if stage['status'] == 'evaluated' else 'insufficient_sample'
        # 导入保持原判据，不事后添加最小经济幅度或升级通过标签。
        if status == 'no_support' and stage['difference']['estimate'] > 0 and stage['difference']['bonferroni_p'] < .05:
            status = 'not_evaluated'
        v = RegimeValidation(run_id=file_sha(root/'completion.json'), spec_sha256=spec.identity, context_id=context,
            status=status, start=protocol['boundary'], end=protocol['end'], test_consumed=protocol['test_consumed'], sealed_oos=protocol['sealed_oos'],
            target_dates=stage.get('high_dates',0), control_dates=stage.get('low_dates',0), unknown_dates=stage.get('missing_dates',0),
            target=stage.get('high'), control=stage.get('low'), difference=stage.get('difference'), unconditional=stage.get('unconditional'),
            family_size=protocol['diagnostic_family_size'], alpha=.05, protocol_sha256=file_sha(root/'protocol.json'),
            source_path=str((root/'factor_results.json').resolve()), source_sha256=file_sha(root/'factor_results.json'),
            interpretation=result['decision']+'；'+result['weak_state_assessment']+'；最小经济幅度本次未评价。',
            supersedes=protocol.get('supersedes_config_sha256'))
        records.append(RegimeRecord(market_id='us_equity', source_candidate_id=card['candidate_id'], factor_id=card['factor_id'],
            hypothesis_sha256=sha256_json(card['original_hypothesis']), mechanism=card['original_hypothesis']['mechanism'],
            origin='historical_addendum', registered_at=protocol['registered_at'], results_seen=True,
            previously_seen=card['previously_seen_in_this_pilot'], context_id=context,
            context_label='五交易日固定开盘收益 · 原28因子完整样本池 · 历史状态比较', spec=spec, validation=v))
    sources = {str((root/n).resolve()):file_sha(root/n) for n in ['completion.json','protocol.json','hypothesis_cards.json','factor_results.json']}
    manifest = RegimeManifest(market_id='us_equity', context_id=context, records=records, source_files=sources)
    save_manifest(manifest, output)
    return manifest
