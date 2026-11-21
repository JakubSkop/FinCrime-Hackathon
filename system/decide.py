"""Aggregate per-alert dispositions into one case decision."""

from __future__ import annotations

from typing import Any

from coverage import close_survives_residual
from matchers import Disposition, explain_alert, match_invoice, prepare_parsed
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


def _payments_fx_held_path(
    bundle: dict[str, Any], policy: InstitutionPolicy, parsed: list[dict[str, Any]]
) -> Disposition | None:
    """First-class decision path centred on the held outbound payment."""
    from evidence_parse import names_overlap

    held = bundle.get("held_payment")
    if not held or bundle.get("institution") != "payments_fx":
        return None

    customer = bundle["customer"]
    amt = float(held.get("amount_gbp") or 0.0)
    cp = held.get("counterparty_name")
    own = names_overlap(cp, customer.get("name"))

    # Own-name held payment: treat as own-account if a proving doc exists.
    if own:
        from matchers import match_own_account

        own_disp = match_own_account(parsed, [held], customer, policy)
        if own_disp:
            return own_disp

    inv = match_invoice(parsed, [held], customer)
    if inv and inv.verdict == "CLOSE":
        return inv
    if inv and inv.verdict == "ESCALATE":
        return inv

    gate = policy.invoice_gate_gbp
    if gate and amt >= gate and not own:
        return Disposition(
            "RFI",
            "RFI_PURPOSE_DOCUMENTS",
            [held["transaction_id"]],
            [
                f"Held payment {held['transaction_id']} of £{amt:,.2f} to {cp} is "
                f"£{gate:,.0f}+ and has no matching invoice or contract on file; ask for "
                f"that invoice/contract before release."
            ],
            weight=78,
        )
    return None


def decide_case(bundle: dict[str, Any]) -> dict[str, Any]:
    policy = get_policy(bundle["institution"])
    parsed = prepare_parsed(bundle)
    case = bundle["case"]

    # Statistical priors (demote / corroborate only — never CLOSE alone).
    from stats import attach_stats

    attach_stats(bundle)

    dispositions: list[Disposition] = []

    # Payments/FX: decide the held payment first, then the alerts.
    held_disp = _payments_fx_held_path(bundle, policy, parsed)
    if held_disp:
        dispositions.append(held_disp)

    dispositions.extend(
        explain_alert(bundle, alert, parsed, policy) for alert in bundle["alerts"]
    )

    # Conservative aggregation.
    if any(d.verdict == "ESCALATE" for d in dispositions):
        esc = [d for d in dispositions if d.verdict == "ESCALATE"]
        code = _pick_code([d.reason_code for d in esc], ESC_PRIORITY)
        chosen = max(
            [d for d in esc if d.reason_code == code] or esc,
            key=lambda d: d.weight,
        )
        verdict = "ESCALATE"
        reason_code = chosen.reason_code
        use = esc + [d for d in dispositions if d.verdict != "ESCALATE"]
    elif dispositions and all(d.verdict == "CLOSE" for d in dispositions):
        residual = close_survives_residual(bundle, dispositions)
        if residual is not None:
            verdict = "RFI"
            reason_code = residual.reason_code
            use = [residual] + list(dispositions)
        else:
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
        verdict = "ESCALATE"
        reason_code = "ESC_UNEXPLAINED_FUNDS"

    evidence_ids: list[str] = []
    for d in use:
        for eid in d.evidence_ids:
            if eid and eid not in evidence_ids:
                evidence_ids.append(eid)
    evidence_ids = _filter_existing_ids(bundle, evidence_ids)

    reason = build_reason(use, verdict, reason_code, bundle=bundle)

    decision = {
        "case_id": case["case_id"],
        "alert_ids": list(case["alert_ids"]),
        "verdict": verdict,
        "reason_code": reason_code,
        "reason": reason,
        "evidence": evidence_ids,
    }

    # Optional polish: may rewrite reason text only.
    try:
        from llm_reason import polish_reason

        decision["reason"] = polish_reason(bundle, decision)
    except Exception:
        pass

    return decision


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
