"""Case-level residual unexplained-money checks before allowing CLOSE."""

from __future__ import annotations

from typing import Any

from matchers import Disposition


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


def explained_amount_gbp(
    bundle: dict[str, Any], dispositions: list[Disposition]
) -> tuple[float, float, list[dict[str, Any]]]:
    """Return (covered, total, unexplained_txs) across alerted transactions."""
    txs = _alert_transactions(bundle)
    total = sum(float(t.get("amount_gbp") or 0.0) for t in txs)
    cited: set[str] = set()
    for d in dispositions:
        cited.update(d.evidence_ids)
    covered_txs = [t for t in txs if t["transaction_id"] in cited]
    covered = sum(float(t.get("amount_gbp") or 0.0) for t in covered_txs)
    unexplained = [t for t in txs if t["transaction_id"] not in cited]
    return covered, total, unexplained


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

    ratio = covered / total
    # Require most of the alerted value to be tied to cited evidence.
    if ratio >= 0.75 or not unexplained:
        return None

    # Small residual (<£500 or <10%) is tolerable when the main story is documented.
    residual = total - covered
    if residual <= 500 or residual / total <= 0.10:
        return None

    sample = unexplained[0]
    return Disposition(
        "RFI",
        "RFI_SOURCE_OF_FUNDS",
        [t["transaction_id"] for t in unexplained[:4]],
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
