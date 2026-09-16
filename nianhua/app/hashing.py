"""确定性序列化与输入哈希工具。"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def canon(obj) -> str:
    """规范化 JSON：键排序、无空白，保证哈希可复现。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_of(obj) -> str:
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()


def short(h: str, n: int = 12) -> str:
    return h[:n]
