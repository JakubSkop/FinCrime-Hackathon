"""Build concise, case-specific reason text from dispositions."""

from __future__ import annotations

from matchers import Disposition


def build_reason(dispositions: list[Disposition], verdict: str, reason_code: str) -> str:
    facts: list[str] = []
    for d in dispositions:
        for fact in d.facts:
            if fact not in facts:
                facts.append(fact)

    if not facts:
        if verdict == "RFI":
            return (
                "Additional information is required before this activity can be cleared. "
                "Ask the customer for the specific documents named in the reason code."
            )
        if verdict == "CLOSE":
            return (
                "The evidence on file explains the alerted activity for this customer "
                "within the review window."
            )
        return (
            "The alerted activity is not credibly explained on the file and is escalated "
            "to the MLRO."
        )

    # Keep 2–4 sentences.
    sentences: list[str] = []
    for fact in facts:
        s = fact.strip()
        if not s.endswith("."):
            s += "."
        sentences.append(s)
        if len(sentences) >= 4:
            break

    if verdict == "RFI" and reason_code.startswith("RFI_"):
        # Ensure an explicit ask is present.
        joined = " ".join(sentences).lower()
        if "ask" not in joined:
            ask = {
                "RFI_PURPOSE_DOCUMENTS": "Ask for documents showing what the payments were for.",
                "RFI_BUSINESS_RECORDS": "Ask for the business records behind the activity.",
                "RFI_SOURCE_OF_FUNDS": "Ask for proof of where the money came from.",
                "RFI_COUNTERPARTY_DETAILS": (
                    "Ask who the counterparty is and what the payment was for."
                ),
                "RFI_UPDATED_PROFILE": (
                    "Ask for an updated expected income or volume declaration."
                ),
            }.get(reason_code)
            if ask:
                sentences.append(ask)
                sentences = sentences[:4]

    return " ".join(sentences[:4])
