"""Optional OpenRouter reason polish. Never changes verdict, reason_code, or evidence."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "baidu/ernie-5.5-flash-preview:free")


def polish_reason(bundle: dict[str, Any], decision: dict[str, Any]) -> str:
    """Return a polished reason, or the original reason if polishing is unavailable."""
    original = decision.get("reason") or ""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return original

    # Keep prompts tiny and deterministic.
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
        "model": DEFAULT_MODEL,
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = (
            body.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, KeyError, IndexError):
        return original

    if not text:
        return original
    # Hard lock: never accept a rewrite that drops required RFI ask language.
    if decision.get("verdict") == "RFI" and "ask" not in text.lower():
        return original
    # Keep length sane.
    if len(text) > 1200:
        return original
    return text
