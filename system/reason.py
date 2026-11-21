"""Build concise, case-specific reason text from dispositions."""

from __future__ import annotations

from typing import Any

from matchers import Disposition


def _ask_for(reason_code: str, bundle: dict[str, Any] | None) -> str:
    txs = []
    if bundle:
        for alert in bundle.get("alerts") or []:
            for tid in alert.get("transaction_ids") or []:
                for t in bundle.get("transactions") or []:
                    if t["transaction_id"] == tid:
                        txs.append(t)
        held = bundle.get("held_payment")
        if held:
            txs.append(held)

    sample = None
    if txs:
        sample = max(txs, key=lambda t: float(t.get("amount_gbp") or 0.0))
    who = (sample or {}).get("counterparty_name") or "the counterparty"
    amt = float((sample or {}).get("amount_gbp") or 0.0)
    tid = (sample or {}).get("transaction_id")
    amt_bit = f" the £{amt:,.2f} payment" if amt else " the alerted payment"
    tid_bit = f" ({tid})" if tid else ""
    with_who = f" to/from {who}" if who and who != "the counterparty" else ""

    asks = {
        "RFI_PURPOSE_DOCUMENTS": (
            f"Ask for the invoice or contract that supports{amt_bit}{with_who}{tid_bit}."
        ),
        "RFI_BUSINESS_RECORDS": (
            f"Ask for business records (takings, invoices, or reconciliations) that break down"
            f"{amt_bit}{with_who}{tid_bit}."
        ),
        "RFI_SOURCE_OF_FUNDS": (
            f"Ask for source-of-funds documents covering{amt_bit}{with_who}{tid_bit}."
        ),
        "RFI_COUNTERPARTY_DETAILS": (
            f"Ask who {who} is and for documents confirming they are not a listed party, "
            f"and what{amt_bit}{tid_bit} was for."
        ),
        "RFI_UPDATED_PROFILE": (
            "Ask for an updated expected income or payment-volume declaration that reflects "
            "the higher own-account activity now seen."
        ),
    }
    return asks.get(
        reason_code,
        "Ask the customer for the specific document that would settle this case.",
    )


def build_reason(
    dispositions: list[Disposition],
    verdict: str,
    reason_code: str,
    bundle: dict[str, Any] | None = None,
) -> str:
    facts: list[str] = []
    for d in dispositions:
        for fact in d.facts:
            if fact not in facts:
                facts.append(fact)

    if not facts:
        if verdict == "RFI":
            return _ask_for(reason_code, bundle)
        if verdict == "CLOSE":
            return (
                "The evidence on file explains the alerted activity for this customer "
                "within the review window."
            )
        return (
            "The alerted activity is not credibly explained on the file and is escalated "
            "to the MLRO."
        )

    sentences: list[str] = []
    for fact in facts:
        s = fact.strip()
        if not s.endswith("."):
            s += "."
        sentences.append(s)
        if len(sentences) >= 3:
            break

    if verdict == "RFI" and reason_code.startswith("RFI_"):
        joined = " ".join(sentences).lower()
        ask = _ask_for(reason_code, bundle)
        if "ask" not in joined:
            sentences.append(ask)
        sentences = sentences[:4]

    # Collapse accidental duplicate ask sentences.
    deduped: list[str] = []
    seen_asks = 0
    for s in sentences:
        is_ask = s.lower().startswith("ask ")
        if is_ask:
            seen_asks += 1
            if seen_asks > 1:
                continue
        deduped.append(s)
    return " ".join(deduped[:4])
