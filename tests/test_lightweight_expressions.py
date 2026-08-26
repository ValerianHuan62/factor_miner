"""批准假设集合单次批量表达式生成测试。"""

from datetime import datetime, timezone
import json
import unittest

from factor_miner.lightweight_expressions import (
    _expected_sign,
    build_lightweight_expression_request,
    candidate_bindings_from_expression_batch,
    parse_lightweight_expression_response,
)
from factor_miner.lightweight_hypotheses import (
    generate_lightweight_hypotheses,
    freeze_lightweight_review,
)
from factor_miner.schema import ExpectedSign
from tests.test_design_diversity import _registry
from tests.test_lightweight_hypotheses import FakeProvider, RUN_ID, decisions, request
from tests.test_research_evolution import (
    context,
    response_payload,
    response_payload_with_semantic_tags,
)


NOW = datetime(2026, 8, 14, 11, tzinfo=timezone.utc)


def reviewed(*, approved_count: int = 2, hypothesis_response=None):
    hypotheses = generate_lightweight_hypotheses(
        run_id=RUN_ID,
        context=context(),
        prepared=request(),
        provider=FakeProvider(hypothesis_response or response_payload()),
    )
    return hypotheses, freeze_lightweight_review(
        hypotheses,
        decisions(hypotheses, approved_count=approved_count),
    )


def field(alias: str) -> dict[str, object]:
    return {"op": "field", "field": alias}


def response(*, invalid_last: bool = False) -> dict[str, object]:
    groups = []
    for hypothesis in (1, 2):
        designs = [
            {
                "candidate_slot_id": f"H{hypothesis:02d}:C001",
                "expression": {
                    "op": "div",
                    "args": [
                        {"op": "delta", "args": [field("price_close")], "period": 20},
                        {"op": "rolling_std", "args": [field("price_close")], "window": 20},
                    ],
                },
            },
            {
                "candidate_slot_id": f"H{hypothesis:02d}:C002",
                "expression": {
                    "op": "mul",
                    "args": [
                        {
                            "op": "div",
                            "args": [
                                field("volume"),
                                {"op": "rolling_mean", "args": [field("volume")], "window": 20},
                            ],
                        },
                        {
                            "op": "div",
                            "args": [
                                field("price_close"),
                                {"op": "rolling_mean", "args": [field("price_close")], "window": 20},
                            ],
                        },
                    ],
                },
            },
            {
                "candidate_slot_id": f"H{hypothesis:02d}:C003",
                "expression": (
                    {"op": "field", "field": "future_return"}
                    if invalid_last and hypothesis == 2
                    else {
                        "op": "neg",
                        "args": [{
                            "op": "rolling_corr",
                            "args": [
                                {"op": "delta", "args": [field("price_close")], "period": 5},
                                {"op": "delta", "args": [field("volume")], "period": 5},
                            ],
                            "window": 20,
                        }],
                    }
                ),
            },
        ]
        groups.append({"logical_slot_id": f"H{hypothesis:02d}", "designs": designs})
    return {"hypotheses": groups}


def response_with_slot_aliases() -> dict[str, object]:
    """模拟模型把两级槽位字段都简写成 slot_id。"""

    payload = response()
    for group in payload["hypotheses"]:
        group["slot_id"] = group.pop("logical_slot_id")
        for design in group["designs"]:
            design["slot_id"] = design.pop("candidate_slot_id")
    return payload


def response_with_common_ast_aliases() -> dict[str, object]:
    """模拟模型使用 type/input/left/right/periods 的常见 AST 方言。"""

    def rewrite(node: dict[str, object]) -> dict[str, object]:
        rewritten = dict(node)
        rewritten["type"] = rewritten.pop("op")
        args = rewritten.pop("args", [])
        if len(args) == 1:
            rewritten["input"] = rewrite(args[0])
        elif len(args) == 2:
            rewritten["left"] = rewrite(args[0])
            rewritten["right"] = rewrite(args[1])
        if "period" in rewritten:
            rewritten["periods"] = rewritten.pop("period")
        return rewritten

    payload = response()
    for group in payload["hypotheses"]:
        for design in group["designs"]:
            design["expression"] = rewrite(design["expression"])
    return payload


class LightweightExpressionsTest(unittest.TestCase):
    """批准集合必须一次请求并保留所有预留槽。"""

    def test_one_request_contains_only_approved_hypotheses(self) -> None:
        hypotheses, review = reviewed(approved_count=2)
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        self.assertEqual(
            prepared.slot_ids,
            tuple(f"H{h:02d}:C{c:03d}" for h in (1, 2) for c in range(1, 4)),
        )
        user_payload = json.loads(prepared.body["messages"][1]["content"])
        encoded = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
        self.assertIn("price_close", encoded)
        self.assertNotIn('"field_id"', encoded)
        self.assertNotIn("rank_ic", encoded)
        self.assertEqual(prepared.body["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", prepared.body)
        self.assertEqual(user_payload["allowed_windows"], [5, 10, 20, 40, 60, 120])
        self.assertEqual(user_payload["minimum_node_count"], 5)
        self.assertEqual(user_payload["minimum_distinct_operator_count"], 3)
        self.assertIn("C001", user_payload["design_roles"])

    def test_expression_request_preserves_approved_semantic_plan(self) -> None:
        hypotheses, review = reviewed(
            approved_count=2,
            hypothesis_response=response_payload_with_semantic_tags(),
        )
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        user_payload = json.loads(prepared.body["messages"][1]["content"])

        self.assertEqual(
            user_payload["hypotheses"][0]["semantic_plan"]["event_tag"],
            "breakout",
        )
        self.assertIn("MACD", prepared.body["messages"][0]["content"])
        self.assertIn("delta 和 delay 必须包含 period", prepared.body["messages"][0]["content"])
        self.assertIn("紧凑函数式 DSL 字符串", prepared.body["messages"][0]["content"])
        self.assertIn("不得为每个节点展开", prepared.body["messages"][0]["content"])

    def test_unambiguous_operator_aliases_are_normalized(self) -> None:
        hypotheses, review = reviewed(approved_count=2)
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        payload = response()
        expression = payload["hypotheses"][0]["designs"][1]["expression"]
        expression["args"][0]["args"][1]["op"] = "roll_mean"
        expression["args"][1] = {
            "op": "mul",
            "args": [
                {"op": "constant", "value": 1},
                expression["args"][1],
            ],
        }
        batch = parse_lightweight_expression_response(
            prepared=prepared,
            response=payload,
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
            created_at=NOW,
        )
        self.assertEqual(batch.ready_slot_count, 6)

    def test_simple_and_macd_like_templates_are_rejected(self) -> None:
        hypotheses, review = reviewed(approved_count=2)
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        payload = response()
        payload["hypotheses"][0]["designs"][0]["expression"] = {
            "op": "rolling_mean", "args": [field("price_close")], "window": 60,
        }
        payload["hypotheses"][0]["designs"][1]["expression"] = {
            "op": "sub",
            "args": [
                {"op": "rolling_mean", "args": [field("price_close")], "window": 20},
                {"op": "rolling_mean", "args": [field("price_close")], "window": 60},
            ],
        }
        batch = parse_lightweight_expression_response(
            prepared=prepared,
            response=payload,
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
            created_at=NOW,
        )
        self.assertEqual(batch.slot_results[0].status, "failed")
        self.assertIn("过于简单", str(batch.slot_results[0].failure_reason))
        self.assertEqual(batch.slot_results[1].status, "failed")
        self.assertIn("MACD", str(batch.slot_results[1].failure_reason))

    def test_group_semantic_error_only_fails_that_hypothesis(self) -> None:
        hypotheses, review = reviewed(approved_count=2)
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        payload = response()
        payload["hypotheses"][0]["designs"][0]["expression"] = {
            "op": "neg",
            "args": [{
                "op": "abs",
                "args": [{
                    "op": "add",
                    "args": [field("price_close"), field("volume")],
                }],
            }],
        }
        batch = parse_lightweight_expression_response(
            prepared=prepared,
            response=payload,
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
            created_at=NOW,
        )
        self.assertEqual(batch.failed_slot_ids[:3], (
            "H01:C001", "H01:C002", "H01:C003",
        ))
        self.assertTrue(all(
            item.status == "ready" for item in batch.slot_results[3:]
        ))

    def test_chinese_expected_direction_requires_explicit_sign_prefix(self) -> None:
        self.assertEqual(_expected_sign("正向：因子越高，未来收益越高"), ExpectedSign.POSITIVE)
        self.assertEqual(_expected_sign("负向：因子越高，未来收益越低"), ExpectedSign.NEGATIVE)
        with self.assertRaisesRegex(ValueError, "必须以正向或负向开头"):
            _expected_sign("与未来收益存在单调关系")

    def test_valid_response_registers_all_six_candidates(self) -> None:
        hypotheses, review = reviewed(approved_count=2)
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        batch = parse_lightweight_expression_response(
            prepared=prepared,
            response=response(),
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
            created_at=NOW,
        )
        self.assertEqual(batch.family_size, 6)
        self.assertEqual(batch.ready_slot_count, 6)
        self.assertEqual(batch.failed_slot_count, 0)
        bindings = candidate_bindings_from_expression_batch(batch)
        self.assertEqual(len(bindings), 6)
        self.assertTrue(all(item.source_candidate_id.startswith("cand_") for item in bindings))

    def test_unambiguous_slot_id_aliases_are_normalized(self) -> None:
        hypotheses, review = reviewed(approved_count=2)
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        batch = parse_lightweight_expression_response(
            prepared=prepared,
            response=response_with_slot_aliases(),
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
            created_at=NOW,
        )
        self.assertEqual(batch.ready_slot_count, 6)
        self.assertEqual(batch.failed_slot_count, 0)

    def test_conflicting_slot_alias_is_rejected(self) -> None:
        hypotheses, review = reviewed(approved_count=2)
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        payload = response_with_slot_aliases()
        payload["hypotheses"][0]["logical_slot_id"] = "H02"
        with self.assertRaisesRegex(ValueError, "槽位字段冲突"):
            parse_lightweight_expression_response(
                prepared=prepared,
                response=payload,
                hypotheses=hypotheses,
                review=review,
                registry=_registry(),
                created_at=NOW,
            )

    def test_common_ast_aliases_are_normalized_before_strict_validation(self) -> None:
        hypotheses, review = reviewed(approved_count=2)
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        batch = parse_lightweight_expression_response(
            prepared=prepared,
            response=response_with_common_ast_aliases(),
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
            created_at=NOW,
        )
        self.assertEqual(batch.ready_slot_count, 6)
        self.assertEqual(batch.failed_slot_count, 0)

    def test_function_style_expression_strings_are_safely_normalized(self) -> None:
        hypotheses, review = reviewed(approved_count=2)
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        payload = response()
        for group in payload["hypotheses"]:
            group["designs"][0]["expression"] = (
                "div(diff(price_close, 20), rolling_std(price_close, 20))"
            )
            group["designs"][1]["expression"] = (
                "mul(div(volume, rolling_mean(volume, 20)), "
                "div(price_close, rolling_mean(price_close, 20)))"
            )
            group["designs"][2]["expression"] = (
                "neg(rolling_corr(delta(price_close, 5), delta(volume, 5), 20))"
            )
        batch = parse_lightweight_expression_response(
            prepared=prepared,
            response=payload,
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
            created_at=NOW,
        )
        self.assertEqual(batch.ready_slot_count, 6)

    def test_function_style_expression_rejects_attribute_calls(self) -> None:
        hypotheses, review = reviewed(approved_count=2)
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        payload = response()
        payload["hypotheses"][0]["designs"][0]["expression"] = "os.system(1)"
        batch = parse_lightweight_expression_response(
            prepared=prepared,
            response=payload,
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
            created_at=NOW,
        )
        self.assertEqual(batch.slot_results[0].status, "failed")
        self.assertIn("白名单", str(batch.slot_results[0].failure_reason))

    def test_invalid_design_keeps_original_slot_failed(self) -> None:
        hypotheses, review = reviewed(approved_count=2)
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        batch = parse_lightweight_expression_response(
            prepared=prepared,
            response=response(invalid_last=True),
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
            created_at=NOW,
        )
        self.assertEqual(batch.family_size, 6)
        self.assertEqual(batch.failed_slot_ids, ("H02:C003",))
        self.assertEqual(len(batch.slot_results), 6)
        bindings = candidate_bindings_from_expression_batch(batch)
        self.assertEqual(len(bindings), 6)
        self.assertEqual(sum(item.terminal_status == "ready" for item in bindings), 5)


if __name__ == "__main__":
    unittest.main()
