"""规范化 JSON 序列化与内容哈希。"""

import hashlib
import json
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    """将 JSON 兼容值确定性地序列化为 UTF-8 字节。

    参数：
        value: 可被标准 JSON 编码器处理的值。

    返回：
        使用固定排序、分隔符和 Unicode 策略编码得到的 UTF-8 字节。
    """

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    """返回规范化 JSON 的小写 SHA-256 摘要。

    参数：
        value: 可被规范化 JSON 编码的值。

    返回：
        完整的小写十六进制 SHA-256 摘要。
    """

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
