"""Turn Claude Code API errors into small, safe user-facing messages."""

from __future__ import annotations

import json
import re
from typing import Any

API_ERROR_PREFIX = "api error:"
API_ERROR_STATUS_PATTERN = re.compile(r"\bAPI Error:\s*(\d{3})\b", re.IGNORECASE)
KNOWN_API_ERROR_PREFIXES = (
    "api error: connection refused",
    "api error: request rejected",
    "api error: stream error:",
    "api error: authentication",
    "api error: unauthorized",
)
UPSTREAM_HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",
    re.IGNORECASE,
)


def _record_text(record: dict[str, Any]) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        parts = [
            block.get("text")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        if parts:
            return "\n".join(parts)
    result = record.get("result")
    return result if isinstance(result, str) else ""


def _error_details(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start < 0:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _retry_after(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if 1 <= seconds <= 86_400 else None


def _upstream_host(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    host = value.strip().lower().rstrip(".")
    return host if UPSTREAM_HOST_PATTERN.fullmatch(host) else None


def api_error_message(record: dict[str, Any], *, allow_text_status: bool = False) -> str | None:
    """Return a safe summary only for records already known to be errors."""
    message = record.get("message")
    message_record = message if isinstance(message, dict) else {}
    text = _record_text(record)
    flagged = record.get("isApiErrorMessage") is True or message_record.get("isApiErrorMessage") is True
    if not flagged and not allow_text_status:
        return None
    status_match = API_ERROR_STATUS_PATTERN.search(text)
    if not flagged and status_match is None:
        return None

    details = _error_details(text)
    status = (
        _status(record.get("apiErrorStatus"))
        or _status(message_record.get("apiErrorStatus"))
        or _status(details.get("status"))
        or _status(details.get("error_code"))
        or (_status(status_match.group(1)) if status_match else None)
    )
    upstream = _upstream_host(details.get("zone"))
    retry_after = _retry_after(details.get("retry_after"))
    context = f"HTTP {status}" if status else "上游请求错误"
    if upstream:
        context += f"，上游 {upstream}"

    if status in {401, 403}:
        return f"Claude Code 上游认证失败（{context}）。请检查当前供应商的密钥或登录状态。"
    if status == 429:
        retry = f"等待至少 {retry_after} 秒后重试" if retry_after else "稍后重试"
        return f"Claude Code 上游请求过于频繁（{context}）。请{retry}。"
    if status is not None and 500 <= status <= 599:
        retry = f"等待至少 {retry_after} 秒后重试" if retry_after else "稍后重试"
        return f"Claude Code 上游线路故障（{context}）。请切换当前供应商/线路，或{retry}。"
    if status is not None:
        return f"Claude Code 请求失败（{context}）。请检查当前供应商、模型和权限设置后重试。"
    return "Claude Code 上游请求失败。请检查当前供应商/线路，或稍后重试。"


def stream_error_state(text: str) -> str:
    """Keep every leading API Error payload private until its record is classified."""
    candidate = text.lstrip().casefold()
    if API_ERROR_PREFIX.startswith(candidate):
        return "pending"
    if candidate.startswith(API_ERROR_PREFIX):
        return "holding"
    return "normal"


def is_known_persisted_api_error(text: str) -> bool:
    """Match historical CLI failures without rewriting normal quoted prose."""
    candidate = text.lstrip().casefold()
    return bool(API_ERROR_STATUS_PATTERN.match(text.lstrip())) or candidate.startswith(KNOWN_API_ERROR_PREFIXES)
