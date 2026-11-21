"""Case-level residual unexplained-money checks before allowing CLOSE.

Thresholds here are policy/materiality rules, not fits to development quirks:
- every alert must cite at least one of its own transactions
- identity/profile closes do not count as explaining payment value
- leftover value may remain only if it is immaterial, or no larger than the
  customer's declared TM01 baseline (ordinary expected activity)
"""

from __future__ import annotations

from typing import Any

from matchers import Disposition

# Immaterial leftover: absolute floor and share of alerted value (same spirit as
# a de-minimis write-off — not tuned to a particular institution's case mix).
_IMMATERIAL_GBP = 500.0
_IMMATERIAL_SHARE = 0.10
# Tiny slack so baseline comparisons are not brittle to rounding in salaries.
_BASELINE_SLACK_GBP = 50.0


def _alert_transactions(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {t["transaction_id"]: t for t in bundle["customer_transactions"]}
    for t in bundle["transactions"]:
        by_id[t["transaction_id"]] = t
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for alert in bundle["alerts"]:
        for tid in alert.get("transaction_ids") or []:
            if tid in seen:
                continue
            seen.add(tid)
            if tid in by_id:
                out.append(by_id[tid])
    held = bundle.get("held_payment")
    if held and held["transaction_id"] not in seen:
        out.append(held)
    return out


# Soft CLOSE codes confirm identity/profile, not that each cited payment is funded.
_SOFT_COVER_CODES = frozenset({"RETURNING_CUSTOMER", "WITHIN_PROFILE"})


def explained_amount_gbp(
    bundle: dict[str, Any], dispositions: list[Disposition]
) -> tuple[float, float, list[dict[str, Any]]]:
    """Return (covered, total, unexplained_txs) across alerted transactions."""
    txs = _alert_transactions(bundle)
    total = sum(float(t.get("amount_gbp") or 0.0) for t in txs)
    cited: set[str] = set()
    for d in dispositions:
        if d.reason_code in _SOFT_COVER_CODES:
            continue
        cited.update(d.evidence_ids)
    covered_txs = [t for t in txs if t["transaction_id"] in cited]
    covered = sum(float(t.get("amount_gbp") or 0.0) for t in covered_txs)
    unexplained = [t for t in txs if t["transaction_id"] not in cited]
    return covered, total, unexplained


def _tm01_baseline(bundle: dict[str, Any]) -> float:
    for alert in bundle.get("alerts") or []:
        if alert.get("rule_id") != "TM01":
            continue
        measured = alert.get("measured") or {}
        baseline = measured.get("baseline_gbp")
        if baseline is not None:
            return float(baseline)
    return 0.0


def _each_alert_touched(
    bundle: dict[str, Any], dispositions: list[Disposition]
) -> list[str]:
    """Return rule_ids for alerts whose transaction legs are entirely uncited.

    Soft identity/profile closes may count as touching an alert, but
    ``explained_amount_gbp`` still ignores them for value coverage.
    """
    cited: set[str] = set()
    for d in dispositions:
        cited.update(d.evidence_ids)
    missing: list[str] = []
    for alert in bundle.get("alerts") or []:
        tids = alert.get("transaction_ids") or []
        if not tids:
            continue
        if not any(tid in cited for tid in tids):
            missing.append(str(alert.get("rule_id") or alert.get("alert_id")))
    return missing


def close_survives_residual(
    bundle: dict[str, Any], dispositions: list[Disposition]
) -> Disposition | None:
    """If CLOSE leaves material unexplained alert value, demote to RFI.

    Returns an RFI disposition to append/replace, or None if CLOSE is fine.
    """
    if not dispositions or any(d.verdict != "CLOSE" for d in dispositions):
        return None

    covered, total, unexplained = explained_amount_gbp(bundle, dispositions)
    if total <= 0:
        return None

    # List-only closes may cite list/register IDs without every tx — allow those.
    codes = {d.reason_code for d in dispositions}
    if codes <= {"LIST_MATCH_DIFFERENT_PARTY"}:
        return None

    # Every alert must touch at least one cited transaction.
    missing_alerts = _each_alert_touched(bundle, dispositions)
    if missing_alerts:
        sample = unexplained[0] if unexplained else None
        detail = (
            f" (e.g. {sample['transaction_id']} £{float(sample['amount_gbp']):,.2f})"
            if sample
            else ""
        )
        return Disposition(
            "RFI",
            "RFI_SOURCE_OF_FUNDS",
            [t["transaction_id"] for t in unexplained[:4]],
            [
                f"Close explanations do not cite transactions for alert(s) "
                f"{', '.join(missing_alerts)}{detail}. Ask for source-of-funds or "
                f"purpose documents covering those alerted legs."
            ],
            weight=68,
        )

    stats = bundle.get("stats") or {}
    novel_ids = set(stats.get("novel_large_tx_ids") or [])
    profile_like = set(stats.get("profile_like_legs") or [])
    periodic = set(stats.get("periodic_legs") or [])
    inbound_vs = float(stats.get("inbound_month_vs_history") or 0.0)

    # Treat periodic / profile-like uncited legs as baseline-shaped for residual math.
    hard_unexplained = [
        t
        for t in unexplained
        if t["transaction_id"] not in profile_like and t["transaction_id"] not in periodic
    ]
    profile_residual = sum(
        float(t.get("amount_gbp") or 0.0)
        for t in unexplained
        if t["transaction_id"] in profile_like or t["transaction_id"] in periodic
    )
    hard_residual = sum(float(t.get("amount_gbp") or 0.0) for t in hard_unexplained)

    # Novel large legs that docs did not cite → always demote.
    novel_uncited = [t for t in unexplained if t["transaction_id"] in novel_ids]
    if novel_uncited:
        sample = novel_uncited[0]
        return Disposition(
            "RFI",
            "RFI_SOURCE_OF_FUNDS",
            [t["transaction_id"] for t in novel_uncited[:4]],
            [
                f"Documents leave a statistically novel payment unexplained "
                f"({sample['transaction_id']} £{float(sample['amount_gbp']):,.2f}"
                + (
                    f" with {sample.get('counterparty_name')}"
                    if sample.get("counterparty_name")
                    else ""
                )
                + "). Ask for source-of-funds or purpose documents for that activity."
            ],
            weight=70,
        )

    if not unexplained:
        return None

    residual = total - covered
    baseline = _tm01_baseline(bundle)

    # Immaterial leftover, or leftover no larger than declared expected activity.
    # Profile-like periodic leftovers can count toward the baseline allowance.
    effective_residual = hard_residual if hard_unexplained else residual
    if residual <= _IMMATERIAL_GBP or residual / total <= _IMMATERIAL_SHARE:
        return None
    if baseline > 0 and residual <= baseline + _BASELINE_SLACK_GBP:
        return None
    if (
        baseline > 0
        and hard_residual <= _IMMATERIAL_GBP
        and profile_residual <= baseline + _BASELINE_SLACK_GBP
    ):
        return None

    # Surge vs historical monthly inbound with material leftover → RFI.
    if inbound_vs >= 2.0 and effective_residual > _IMMATERIAL_GBP:
        sample = (hard_unexplained or unexplained)[0]
        return Disposition(
            "RFI",
            "RFI_SOURCE_OF_FUNDS",
            [t["transaction_id"] for t in (hard_unexplained or unexplained)[:4]],
            [
                f"Inbound activity in the alert window is about {inbound_vs:.1f}× this "
                f"customer's historical monthly median, and £{effective_residual:,.2f} "
                f"remains unexplained (e.g. {sample['transaction_id']} "
                f"£{float(sample['amount_gbp']):,.2f}). Ask for source-of-funds documents "
                f"covering the excess."
            ],
            weight=69,
        )

    sample = (hard_unexplained or unexplained)[0]
    return Disposition(
        "RFI",
        "RFI_SOURCE_OF_FUNDS",
        [t["transaction_id"] for t in (hard_unexplained or unexplained)[:4]],
        [
            f"Documents explain £{covered:,.2f} of £{total:,.2f} alerted value, but "
            f"£{residual:,.2f} remains unexplained (e.g. {sample['transaction_id']} "
            f"£{float(sample['amount_gbp']):,.2f}"
            + (
                f" with {sample.get('counterparty_name')}"
                if sample.get("counterparty_name")
                else ""
            )
            + "). "
            f"Ask for source-of-funds or purpose documents covering the unexplained remainder."
        ],
        weight=66,
    )
