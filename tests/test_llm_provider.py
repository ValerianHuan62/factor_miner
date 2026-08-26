"""V0.5 DeepSeek 传输、逐字录制和密钥安全测试。"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_online import LLMExportAuthorization
from factor_miner.llm_provider import (
    execute_recorded_call,
    replay_recorded_call,
)
from tests.test_llm_online import policy, prepared_request


NOW = datetime(2026, 7, 30, 8, tzinfo=timezone.utc)
SECRET = "synthetic-secret-never-persist-0123456789"


class FakeTransport:
    """捕获请求并返回一个不含推理链的合成 DeepSeek 响应。"""

    def __init__(self, response: bytes | None = None) -> None:
        self.calls: list[tuple[bytes, str]] = []
        self.response = response or json.dumps(
            {
                "id": "response-synthetic",
                "system_fingerprint": "fp-synthetic",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"status":"ok","value":1}',
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "prompt_tokens_details": {"cached_tokens": 2},
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def post(self, request_bytes: bytes, api_key: str) -> bytes:
        """保存调用参数并返回固定响应。"""

        self.calls.append((request_bytes, api_key))
        return self.response


def valid_authorization():
    """构造当前有效的精确请求授权。"""

    request = prepared_request()
    return LLMExportAuthorization(
        authorization_id="authorization-synthetic-provider",
        campaign_id=request.campaign_id,
        request_sha256=request.request_sha256,
        corporate_policy_id=policy().policy_id,
        approver_role="research_owner",
        authorized_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


class LLMProviderTest(unittest.TestCase):
    """传输层只发送已授权字节，并能发现响应或录制篡改。"""

    def test_authorized_call_records_exact_bytes_without_secret(self) -> None:
        request = prepared_request()
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": SECRET}):
                result = execute_recorded_call(
                    prepared=request,
                    authorization=valid_authorization(),
                    policy=policy(),
                    record_root=Path(directory),
                    transport=transport,
                    now=NOW,
                )

            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(transport.calls[0][1], SECRET)
            self.assertEqual(result.content_json, {"status": "ok", "value": 1})
            replayed = replay_recorded_call(result.record_directory)
            self.assertEqual(replayed, result)

            persisted = b"".join(
                path.read_bytes()
                for path in result.record_directory.iterdir()
                if path.is_file()
            )
            self.assertNotIn(SECRET.encode("utf-8"), persisted)

    def test_missing_key_fails_without_calling_transport(self) -> None:
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(FactorMinerError) as context:
                    execute_recorded_call(
                        prepared=prepared_request(),
                        authorization=valid_authorization(),
                        policy=policy(),
                        record_root=Path(directory),
                        transport=transport,
                        now=NOW,
                    )

        self.assertEqual(
            context.exception.code,
            FailureCode.LLM_PROVIDER_UNAVAILABLE,
        )
        self.assertNotIn(SECRET, str(context.exception))
        self.assertEqual(transport.calls, [])

    def test_invalid_response_and_tampered_replay_fail_closed(self) -> None:
        invalid = FakeTransport(b'{"choices":[]}')
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": SECRET}):
                with self.assertRaises(FactorMinerError) as context:
                    execute_recorded_call(
                        prepared=prepared_request(),
                        authorization=valid_authorization(),
                        policy=policy(),
                        record_root=Path(directory),
                        transport=invalid,
                        now=NOW,
                    )
            self.assertEqual(
                context.exception.code,
                FailureCode.LLM_RESPONSE_INVALID,
            )
            self.assertNotIn(SECRET, str(context.exception))

            calls_root = Path(directory) / "calls"
            call_directories = tuple(calls_root.iterdir())
            self.assertEqual(len(call_directories), 1)
            self.assertTrue((call_directories[0] / "request.json").is_file())
            self.assertTrue((call_directories[0] / "response.json").is_file())
            invalid_record = json.loads(
                (call_directories[0] / "call_record.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(invalid_record["finish_reason"], "invalid_response")
            persisted = b"".join(
                path.read_bytes()
                for path in call_directories[0].iterdir()
                if path.is_file()
            )
            self.assertNotIn(SECRET.encode("utf-8"), persisted)

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": SECRET}):
                result = execute_recorded_call(
                    prepared=prepared_request(),
                    authorization=valid_authorization(),
                    policy=policy(),
                    record_root=Path(directory),
                    transport=FakeTransport(),
                    now=NOW,
                )
            response_path = result.record_directory / "response.json"
            response_path.write_bytes(response_path.read_bytes() + b" ")
            with self.assertRaises(FactorMinerError) as tamper_context:
                replay_recorded_call(result.record_directory)
            self.assertEqual(
                tamper_context.exception.code,
                FailureCode.LEDGER_CORRUPT,
            )

    def test_tool_call_response_is_recorded_without_pretending_final_json(self) -> None:
        response = json.dumps(
            {
                "id": "response-tool-synthetic",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "synthetic hidden reasoning",
                            "tool_calls": [
                                {
                                    "id": "call_crossref_1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_literature",
                                        "arguments": (
                                            '{"query_terms":["price","continuation"],'
                                            '"year_start":1990,"year_end":2026,'
                                            '"result_limit":3}'
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "prompt_tokens_details": {"cached_tokens": 2},
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": SECRET}):
                result = execute_recorded_call(
                    prepared=prepared_request(),
                    authorization=valid_authorization(),
                    policy=policy(),
                    record_root=Path(directory),
                    transport=FakeTransport(response),
                    now=NOW,
                )

            self.assertIsNone(result.content_json)
            self.assertEqual(len(result.tool_calls), 1)
            self.assertEqual(
                result.tool_calls[0].name,
                "search_literature",
            )
            self.assertEqual(
                result.record.usage,
                {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            )
            self.assertEqual(replay_recorded_call(result.record_directory), result)


if __name__ == "__main__":
    unittest.main()
