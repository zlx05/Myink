"""Bounded, best-effort copies for inspection; never alter generation inputs.

Limits apply to the encoded data as well as individual values. Unknown objects
are discarded, not repr()'d: exceptions/provider clients may contain credentials.
This is credential scrubbing, not a claim to detect arbitrary secrets in prose.
"""
from __future__ import annotations

import json
import math
import re
from itertools import islice

LIMITS = {"max_depth": 10, "max_items": 80, "max_text": 16000, "max_bytes": 65536}
# These pre-existing fields are a business store, not just diagnostics: manual
# resume and reflexion read them back. Preserve them exactly at rest. Admin
# responses still call capture() on the ENTIRE detail, without this exception.
_CONTROL_FIELDS = frozenset({"plan", "plan_attempt", "writing_mode", "audit_verdict"})
_REDACTED = "[REDACTED]"
_TRUNCATED = "[TRUNCATED]"
_KEY = re.compile(r"password|passwd|pwd|hash|apikey|accesstoken|refreshtoken|secret|authorization|credential|connectionstring", re.I)
_BEARER = re.compile(r"\b(?:Bearer|Basic|API[-_ ]?Key)\s+[A-Za-z0-9._~+/=-]+", re.I)
_ASSIGNMENT = re.compile(r'''(?i)(["']?(?:password|passwd|pwd|api[-_]?key|access[-_]?token|refresh[-_]?token|token|secret|authorization)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''')
# Locate delimiters independently: an authority must never consume the next URL
# candidate's scheme delimiter in compact serialized text. Both scans are linear.
_URL_START = re.compile(r"://")
_URL_AUTHORITY = re.compile(r'[^\s/?#"]*')
_KEY_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")


def _secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized in {"inputtokens", "outputtokens", "totaltokens", "cachedtokens"}:
        return False
    return bool(_KEY.search(normalized)) or normalized.endswith("token") or normalized in {"tokens", "modelroutes", "environment"}


def _redact_urls(value: str) -> str:
    starts = _URL_START.finditer(value)
    current = next(starts, None)
    parts = []
    cursor = 0
    while current is not None:
        following = next(starts, None)
        boundary = following.start() if following is not None else len(value)
        # Anchored, non-overlapping windows keep total scan work proportional to
        # input length, even with long prefixes or many immediately adjacent URLs.
        authority = _URL_AUTHORITY.match(value, current.end(), boundary)
        separator = value.rfind("@", current.end(), authority.end())
        if separator >= 0:
            parts.extend((value[cursor:current.end()], "[REDACTED]"))
            cursor = separator
        current = following
    parts.append(value[cursor:])
    return "".join(parts)


def scrub_text(value: str) -> str:
    if "://" in value:
        value = _redact_urls(value)
    value = _BEARER.sub(_REDACTED, value)
    value = _ASSIGNMENT.sub(lambda m: m.group(1) + _REDACTED, value)
    return _KEY_TOKEN.sub(_REDACTED, value)


def capture(value: object, *, max_bytes: int = 65536) -> dict:
    """Return JSON data plus explicit redaction/truncation metadata; fail closed."""
    truncated = False
    redacted = False
    limits = {**LIMITS, "max_bytes": min(max_bytes, LIMITS["max_bytes"])}
    remaining = limits["max_bytes"] - 512

    def walk(item, depth):
        nonlocal remaining, truncated, redacted
        if depth > LIMITS["max_depth"] or remaining < 64:
            truncated = True
            return _TRUNCATED
        if item is None or isinstance(item, (bool, int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                return None
            remaining -= 24
            return item
        if isinstance(item, str):
            # Scrub before clipping to avoid retaining a partial credential.
            cleaned = scrub_text(item)
            redacted |= cleaned != item
            max_chars = min(LIMITS["max_text"], max(0, (remaining - 64) // 6))
            if len(cleaned) > max_chars:
                cleaned = cleaned[:max_chars] + _TRUNCATED
                truncated = True
            remaining -= len(json.dumps(cleaned, ensure_ascii=True).encode())
            return cleaned
        if isinstance(item, dict):
            result = {}
            remaining -= 2
            for index, (key, child) in enumerate(islice(item.items(), LIMITS["max_items"] + 1)):
                if index == LIMITS["max_items"] or remaining < 128:
                    truncated = True
                    break
                if not isinstance(key, str):
                    truncated = True
                    continue
                safe_key = scrub_text(key[:256])
                if len(key) > 256:
                    truncated = True
                remaining -= len(json.dumps(safe_key).encode()) + 4
                if _secret_key(key):
                    result[safe_key] = _REDACTED
                    redacted = True
                    remaining -= 16
                else:
                    result[safe_key] = walk(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            result = []
            remaining -= 2
            for index, child in enumerate(islice(item, LIMITS["max_items"] + 1)):
                if index == LIMITS["max_items"] or remaining < 128:
                    truncated = True
                    break
                remaining -= 2
                result.append(walk(child, depth + 1))
            return result
        truncated = True
        return "[UNAVAILABLE]"

    try:
        data = walk(value, 0)
        if len(json.dumps(data, ensure_ascii=True).encode()) > limits["max_bytes"] - 512:
            data, truncated = _TRUNCATED, True
        return {"data": data, "truncated": truncated, "redacted": redacted, "limits": limits}
    except Exception:
        return {"data": "[UNAVAILABLE]", "truncated": True, "redacted": True, "limits": limits}


def capture_detail(detail: dict | None) -> dict | None:
    """Bound added telemetry while preserving the existing business-control store.

    The 64KiB limit applies only to telemetry; the four explicitly allowlisted
    legacy control fields remain lossless, including on later detail merges.
    Inspection must use capture(), which also scrubs/bounds those control fields.
    """
    if detail is None:
        return None
    controls = {key: detail[key] for key in _CONTROL_FIELDS if key in detail}
    try:
        # Reserve space for both request and response and later tool merges.
        budgets = {"tool_trace": 6000, "response": 18000, "messages": 30000}
        captured = {key: capture(detail[key], max_bytes=size) for key, size in budgets.items() if key in detail}
        other_budget = LIMITS["max_bytes"] - 512 - sum(budgets[key] for key in captured)
        other = capture({key: value for key, value in islice(detail.items(), LIMITS["max_items"])
                         if key not in budgets and key not in _CONTROL_FIELDS and key != "_capture"}, max_bytes=other_budget)
        data = other["data"] if isinstance(other["data"], dict) else {}
        data.update({key: result["data"] for key, result in captured.items()})
        previous = detail.get("_capture") or {}
        reports = [other, *captured.values(), previous if isinstance(previous, dict) else {}]
        data["_capture"] = {"scope": "telemetry_only",
                            "truncated": len(detail) > LIMITS["max_items"] or any(r.get("truncated") for r in reports),
                            "redacted": any(r.get("redacted") for r in reports), "limits": dict(LIMITS)}
        data.update(controls)
        return data
    except Exception:
        # A diagnostic failure must not discard plan versions or audit findings.
        return {**controls, "_capture": {"scope": "telemetry_only", "truncated": True,
                                         "redacted": True, "limits": dict(LIMITS)}}
