#!/usr/bin/env python3
"""OpenRouter polish must fall back to the rule-written reason."""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
from contextlib import redirect_stderr
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "system"))

import llm_reason  # noqa: E402


DECISION = {
    "case_id": "CASE-1",
    "verdict": "CLOSE",
    "reason_code": "INVOICE_MATCHES",
    "reason": "Invoice INV-1 matches the £10.00 payment.",
    "evidence": ["INV-1"],
}
BUNDLE = {"customer": {"name": "Ada Example"}, "alerts": [{"message": "TM01"}]}


def _ok_body(text: str) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": text}}]}
    ).encode("utf-8")


class _Resp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _run(fn):
    llm_reason.reset_polish_state()
    saved = {
        key: os.environ.get(key)
        for key in ("OPENROUTER_API_KEY", "OPENROUTER_POLISH")
    }
    try:
        fn()
    finally:
        llm_reason.reset_polish_state()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_no_key_keeps_default():
    def check():
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("OPENROUTER_POLISH", None)
        with patch("llm_reason.urllib.request.urlopen") as urlopen:
            got = llm_reason.polish_reason(BUNDLE, DECISION)
        assert got == DECISION["reason"]
        urlopen.assert_not_called()

    _run(check)


def test_explicit_off_keeps_default():
    def check():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        os.environ["OPENROUTER_POLISH"] = "0"
        with patch("llm_reason.urllib.request.urlopen") as urlopen:
            got = llm_reason.polish_reason(BUNDLE, DECISION)
        assert got == DECISION["reason"]
        urlopen.assert_not_called()

    _run(check)


def test_http_error_falls_back_and_stops_retrying():
    def check():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        os.environ.pop("OPENROUTER_POLISH", None)
        calls = {"n": 0}

        def fail(*args, **kwargs):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                llm_reason.OPENROUTER_URL, 401, "unauthorized", hdrs=None, fp=io.BytesIO(b"{}")
            )

        err = io.StringIO()
        with redirect_stderr(err), patch("llm_reason.urllib.request.urlopen", side_effect=fail):
            first = llm_reason.polish_reason(BUNDLE, DECISION)
            second = llm_reason.polish_reason(BUNDLE, DECISION)
        assert first == DECISION["reason"]
        assert second == DECISION["reason"]
        assert calls["n"] == 1
        assert "unavailable" in err.getvalue()
        assert not llm_reason.polish_enabled()

    _run(check)


def test_timeout_falls_back():
    def check():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        os.environ.pop("OPENROUTER_POLISH", None)

        def fail(*args, **kwargs):
            raise TimeoutError("timed out")

        with redirect_stderr(io.StringIO()), patch(
            "llm_reason.urllib.request.urlopen", side_effect=fail
        ):
            got = llm_reason.polish_reason(BUNDLE, DECISION)
        assert got == DECISION["reason"]
        assert not llm_reason.polish_enabled()

    _run(check)


def test_success_rewrites_text_only():
    def check():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        os.environ.pop("OPENROUTER_POLISH", None)
        polished = "Invoice INV-1 for £10.00 matches the alerted payment. The case can close."

        with patch(
            "llm_reason.urllib.request.urlopen",
            return_value=_Resp(_ok_body(polished)),
        ):
            got = llm_reason.polish_reason(BUNDLE, DECISION)
        assert got == polished

    _run(check)


def test_rfi_without_ask_keeps_default():
    def check():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        os.environ.pop("OPENROUTER_POLISH", None)
        decision = dict(DECISION)
        decision["verdict"] = "RFI"
        decision["reason"] = "Ask for the invoice that supports the £10.00 payment."
        with patch(
            "llm_reason.urllib.request.urlopen",
            return_value=_Resp(_ok_body("The payment is unexplained.")),
        ):
            got = llm_reason.polish_reason(BUNDLE, decision)
        assert got == decision["reason"]
        assert llm_reason.polish_enabled()

    _run(check)


def main() -> int:
    tests = [
        test_no_key_keeps_default,
        test_explicit_off_keeps_default,
        test_http_error_falls_back_and_stops_retrying,
        test_timeout_falls_back,
        test_success_rewrites_text_only,
        test_rfi_without_ask_keeps_default,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"polish_fallback: {len(tests)}/{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
