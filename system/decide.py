"""Aggregate per-alert dispositions into one case decision."""

from __future__ import annotations

from typing import Any

from matchers import Disposition, explain_alert, prepare_parsed
from policy import InstitutionPolicy, get_policy, validate_reason_code
from reason import build_reason


ESC_PRIORITY = [
    "ESC_LISTED_PARTY",
    "ESC_MULE_ACTIVITY",
    "ESC_ACCOUNT_MISUSE",
    "ESC_PASS_THROUGH",
    "ESC_STRUCTURING",
    "ESC_EARLY_REDEMPTION",
    "ESC_THIRD_PARTY_FUNDING",
    "ESC_INVOICE_MISMATCH",
    "ESC_HIGH_RISK_CORRIDOR",
    "ESC_UNEXPLAINED_FUNDS",
]

CLOSE_PRIORITY = [
    "LIST_MATCH_DIFFERENT_PARTY",
    "INVOICE_MATCHES",
    "OWN_ACCOUNT",
    "GROUP_COMPANY",
    "COLLECTION_DOCUMENTED",
    "BUSINESS_RECORDS_RECONCILE",
    "DOCUMENTED_EVENT",
    "THIRD_PARTY_DOCUMENTED",
    "REFUND_OF_EARLIER_PAYMENT",
    "RETURNING_CUSTOMER",
    "WITHIN_PROFILE",
]

RFI_PRIORITY = [
    "RFI_UPDATED_PROFILE",
    "RFI_COUNTERPARTY_DETAILS",
    "RFI_PURPOSE_DOCUMENTS",
    "RFI_BUSINESS_RECORDS",
    "RFI_SOURCE_OF_FUNDS",
]


def _pick_code(codes: list[str], order: list[str]) -> str:
    rank = {c: i for i, c in enumerate(order)}
    return sorted(codes, key=lambda c: rank.get(c, 999))[0]


def _payments_fx_invoice_gate(
    bundle: dict[str, Any], policy: InstitutionPolicy, parsed: list[dict[str, Any]]
) -> Disposition | None:
    """Extra case-level gate for held outbound payments."""
    from evidence_parse import names_overlap
    from matchers import match_invoice

    held = bundle.get("held_payment")
    if not held or not policy.invoice_gate_gbp:
        return None
    amt = float(held.get("amount_gbp") or 0.0)
    if amt < policy.invoice_gate_gbp:
        return None
    customer = bundle["customer"]
    if names_overlap(held.get("counterparty_name"), customer.get("name")):
        return None
    inv = match_invoice(parsed, [held], customer)
    if inv and inv.verdict == "CLOSE":
        return inv
    if inv and inv.verdict == "ESCALATE":
        return inv
    return Disposition(
        "RFI",
        "RFI_PURPOSE_DOCUMENTS",
        [held["transaction_id"]],
        [
            f"Held payment {held['transaction_id']} of £{amt:,.2f} to "
            f"{held.get('counterparty_name')} needs an invoice or contract on file "
            f"before release."
        ],
        weight=70,
    )


def decide_case(bundle: dict[str, Any]) -> dict[str, Any]:
    policy = get_policy(bundle["institution"])
    parsed = prepare_parsed(bundle)
    case = bundle["case"]

    dispositions = [explain_alert(bundle, alert, parsed, policy) for alert in bundle["alerts"]]

    gate = _payments_fx_invoice_gate(bundle, policy, parsed)
    if gate:
        dispositions.append(gate)

    # Conservative aggregation.
    if any(d.verdict == "ESCALATE" for d in dispositions):
        esc = [d for d in dispositions if d.verdict == "ESCALATE"]
        primary = max(esc, key=lambda d: d.weight)
        # Prefer priority list if weights tie-ish
        code = _pick_code([d.reason_code for d in esc], ESC_PRIORITY)
        # Keep the facts from the highest-weight escalate with that code if possible.
        chosen = max(
            [d for d in esc if d.reason_code == code] or esc,
            key=lambda d: d.weight,
        )
        verdict = "ESCALATE"
        reason_code = chosen.reason_code
        use = esc + [d for d in dispositions if d.verdict != "ESCALATE"]
    elif dispositions and all(d.verdict == "CLOSE" for d in dispositions):
        verdict = "CLOSE"
        code = _pick_code([d.reason_code for d in dispositions], CLOSE_PRIORITY)
        chosen = max(
            [d for d in dispositions if d.reason_code == code],
            key=lambda d: d.weight,
        )
        reason_code = chosen.reason_code
        use = dispositions
    elif any(d.verdict == "RFI" for d in dispositions) and all(
        d.verdict in {"CLOSE", "RFI"} for d in dispositions
    ):
        verdict = "RFI"
        rfis = [d for d in dispositions if d.verdict == "RFI"]
        code = _pick_code([d.reason_code for d in rfis], RFI_PRIORITY)
        chosen = max([d for d in rfis if d.reason_code == code], key=lambda d: d.weight)
        reason_code = chosen.reason_code
        # Prefer RFI facts so CLOSE side-notes do not muddy the ask.
        use = rfis + [d for d in dispositions if d.verdict == "CLOSE"]
    else:
        verdict = "ESCALATE"
        reason_code = "ESC_UNEXPLAINED_FUNDS"
        use = dispositions or [
            Disposition(
                "ESCALATE",
                "ESC_UNEXPLAINED_FUNDS",
                [],
                ["No disposition could be formed for this case."],
                1,
            )
        ]

    if not validate_reason_code(verdict, reason_code):
        # Fail closed.
        verdict = "ESCALATE"
        reason_code = "ESC_UNEXPLAINED_FUNDS"

    evidence_ids: list[str] = []
    for d in use:
        for eid in d.evidence_ids:
            if eid and eid not in evidence_ids:
                # Only keep IDs that exist in this institution folder context.
                evidence_ids.append(eid)

    evidence_ids = _filter_existing_ids(bundle, evidence_ids)

    reason = build_reason(use, verdict, reason_code)

    return {
        "case_id": case["case_id"],
        "alert_ids": list(case["alert_ids"]),
        "verdict": verdict,
        "reason_code": reason_code,
        "reason": reason,
        "evidence": evidence_ids,
    }


def _filter_existing_ids(bundle: dict[str, Any], ids: list[str]) -> list[str]:
    valid: set[str] = set()
    for doc in bundle["evidence"]:
        valid.add(doc["doc_id"])
    for tx in bundle["customer_transactions"]:
        valid.add(tx["transaction_id"])
    for tx in bundle["transactions"]:
        valid.add(tx["transaction_id"])
    for entry_id in bundle["internal_list"].keys():
        valid.add(entry_id)
    for rev in bundle["previous_reviews"]:
        if rev.get("review_id"):
            valid.add(rev["review_id"])
    held = bundle.get("held_payment")
    if held:
        valid.add(held["transaction_id"])
    return [i for i in ids if i in valid]
