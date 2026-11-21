#!/usr/bin/env python3
"""Smoke asserts for per-customer statistical features (stdlib only)."""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "system"))

from loader import InstitutionData  # noqa: E402
from stats import attach_stats, build_case_features  # noqa: E402


def _find_case(data: InstitutionData, pred):
    for case in data.cases:
        bundle = data.assemble_case(case)
        if pred(bundle):
            return bundle
    raise AssertionError("No matching case found for smoke predicate")


def test_features_deterministic():
    data = InstitutionData(os.path.join(ROOT, "institutions", "bank"))
    bundle = data.assemble_case(data.cases[0])
    a = build_case_features(bundle)
    b = build_case_features(bundle)
    assert a == b, "feature bag must be deterministic"
    assert "alert_amount_scores" in a
    assert "novel_large_tx_ids" in a
    assert "periodic_legs" in a
    assert a["history_tx_count"] >= 0


def test_attach_on_bundle():
    data = InstitutionData(os.path.join(ROOT, "institutions", "bank"))
    bundle = data.assemble_case(data.cases[0])
    feats = attach_stats(bundle)
    assert bundle.get("stats") is feats


def test_history_excludes_review_window():
    data = InstitutionData(os.path.join(ROOT, "institutions", "bank"))
    bundle = data.assemble_case(data.cases[0])
    window_from = (bundle["case"].get("review_window") or {}).get("from")
    assert window_from
    feats = build_case_features(bundle)
    # History count should be less than full customer tx count when window cuts.
    full = len(bundle["customer_transactions"])
    assert feats["history_tx_count"] <= full


def test_novel_or_periodic_signals_exist_somewhere():
    """Across a sample of bank cases, novelty and periodicity signals appear."""
    data = InstitutionData(os.path.join(ROOT, "institutions", "bank"))
    saw_novel = False
    saw_periodic_leg = False
    saw_profile_like = False
    for case in data.cases[:300]:
        feats = build_case_features(data.assemble_case(case))
        if feats.get("novel_large_tx_ids"):
            saw_novel = True
        if feats.get("periodic_legs"):
            saw_periodic_leg = True
        if feats.get("profile_like_legs"):
            saw_profile_like = True
        if saw_novel and (saw_periodic_leg or saw_profile_like):
            break
    assert saw_novel, "expected some novel-large alerted legs in first 300 bank cases"
    assert saw_periodic_leg or saw_profile_like, (
        "expected some periodic or profile-like legs in first 300 bank cases"
    )


def test_emoney_and_pfx_run():
    for inst in ("emoney", "payments_fx", "investment"):
        data = InstitutionData(os.path.join(ROOT, "institutions", inst))
        bundle = data.assemble_case(data.cases[0])
        feats = attach_stats(bundle)
        assert isinstance(feats["inbound_month_vs_history"], float)


def main() -> int:
    test_features_deterministic()
    test_attach_on_bundle()
    test_history_excludes_review_window()
    test_novel_or_periodic_signals_exist_somewhere()
    test_emoney_and_pfx_run()
    print("stats_smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
