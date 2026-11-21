"""Optional OpenRouter reason polish. Never changes verdict, reason_code, or evidence.

The case decision is always produced by the rules first. Polishing may replace
only the reason sentence, and only when OpenRouter returns usable text. If the
key is unset, the request fails, or the model is unavailable, the rule-written
reason is kept and OpenRouter is not called again in this process.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "baidu/ernie-5.5-flash-preview:free")
_TIMEOUT_SECONDS = 10

_disabled = False
_announced = False


def reset_polish_state() -> None:
    """Clear the process-wide fallback latch. Used by tests."""
    global _disabled, _announced
    _disabled = False
    _announced = False


def _polish_requested() -> bool:
    flag = (os.environ.get("OPENROUTER_POLISH") or "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    return bool((os.environ.get("OPENROUTER_API_KEY") or "").strip())


def polish_enabled() -> bool:
    """True when this process will attempt an OpenRouter rewrite."""
    return _polish_requested() and not _disabled


def _disable(reason: str) -> None:
    global _disabled, _announced
    _disabled = True
    if _announced:
        return
    _announced = True
    print(
        "OpenRouter reason polish is unavailable "
        f"({reason}). Keeping the default rule-written reasons for this run.",
        file=sys.stderr,
    )


def polish_reason(bundle: dict[str, Any], decision: dict[str, Any]) -> str:
    """Return a polished reason, or the original reason if polishing is unavailable."""
    original = decision.get("reason") or ""
    if not polish_enabled():
        return original

    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    facts = {
        "case_id": decision.get("case_id"),
        "verdict": decision.get("verdict"),
        "reason_code": decision.get("reason_code"),
        "evidence_ids": decision.get("evidence") or [],
        "draft_reason": original,
        "customer_name": (bundle.get("customer") or {}).get("name"),
        "alert_messages": [a.get("message") for a in bundle.get("alerts") or []],
    }
    system = (
        "You rewrite financial crime alert rationales. "
        "Keep the same verdict and reason_code. "
        "Use only the provided evidence IDs and facts. "
        "Write 2 to 4 sentences. For RFI, end with one specific ask. "
        "Do not invent documents, amounts, or parties. "
        "Return only the reason text."
    )
    user = json.dumps(facts, ensure_ascii=False, sort_keys=True)
    payload = {
        "model": os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/JakubSkop/FinCrime-Hackathon",
            "X-Title": "FinCrime alert-closing system",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        body = json.loads(raw)
    except urllib.error.HTTPError as exc:
        _disable(f"HTTP {exc.code}")
        return original
    except Exception as exc:
        _disable(type(exc).__name__)
        return original

    if not isinstance(body, dict):
        _disable("invalid response")
        return original
    if body.get("error"):
        err = body["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        _disable((message or "API error")[:160])
        return original

    try:
        choice = (body.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content", "")
    except Exception:
        _disable("invalid response")
        return original

    text = _content_text(content)
    if not text:
        return original
    # A rewrite that drops the required RFI ask is rejected for this case only.
    if decision.get("verdict") == "RFI" and "ask" not in text.lower():
        return original
    if len(text) > 1200:
        return original
    return text


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
        return " ".join(p for p in parts if p).strip()
    return ""
