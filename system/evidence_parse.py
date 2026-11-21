"""Parse structured fields from evidence markdown documents."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any


AMOUNT_RE = re.compile(r"£\s*([\d,]+(?:\.\d{1,2})?)")
LABEL_RE = re.compile(r"\*\*([^*]+):\*\*\s*(.+)")
ACCOUNT_RE = re.compile(
    r"(?:account\s*)?(\d{2}-\d{2}-\d{2})\s+(\d{6,8})", re.IGNORECASE
)
DATE_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
DATE_LONG_RE = re.compile(
    r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4})\b",
    re.IGNORECASE,
)


def parse_amount(text: str | None) -> float | None:
    if not text:
        return None
    m = AMOUNT_RE.search(text)
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def all_amounts(text: str) -> list[float]:
    return [float(m.group(1).replace(",", "")) for m in AMOUNT_RE.finditer(text or "")]


def norm_name(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    # Drop common company suffixes for softer matching.
    value = re.sub(
        r"\b(limited|ltd|llp|plc|inc|corp|company|co)\b", " ", value
    )
    return re.sub(r"\s+", " ", value).strip()


def names_overlap(a: str | None, b: str | None) -> bool:
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    ta, tb = set(na.split()), set(nb.split())
    if len(ta) >= 2 and len(tb) >= 2 and len(ta & tb) >= 2:
        return True
    return False


def parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text[:32], fmt)
        except ValueError:
            continue
    m = DATE_ISO_RE.search(text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            pass
    m = DATE_LONG_RE.search(text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%d %B %Y")
        except ValueError:
            try:
                return datetime.strptime(m.group(1), "%d %b %Y")
            except ValueError:
                return None
    return None


def days_between(a: str | datetime | None, b: str | datetime | None) -> int | None:
    da = a if isinstance(a, datetime) else parse_date(str(a) if a else None)
    db = b if isinstance(b, datetime) else parse_date(str(b) if b else None)
    if not da or not db:
        return None
    return abs((da.date() - db.date()).days)


def extract_labels(text: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for m in LABEL_RE.finditer(text or ""):
        key = m.group(1).strip().lower()
        val = m.group(2).strip()
        labels[key] = val
    return labels


def extract_accounts(text: str) -> list[str]:
    return [f"{m.group(1)} {m.group(2)}" for m in ACCOUNT_RE.finditer(text or "")]


def label_amount(labels: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        for lk, lv in labels.items():
            if key in lk:
                amt = parse_amount(lv)
                if amt is not None:
                    return amt
    return None


def label_value(labels: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        for lk, lv in labels.items():
            if key in lk:
                return lv
    return None


def parse_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized view of one evidence document."""
    text = doc.get("text") or ""
    labels = extract_labels(text)
    amounts = all_amounts(text)
    primary = label_amount(
        labels,
        "amount due",
        "amount",
        "fee",
        "principal amount",
        "sale price",
        "paid to client's investment account",
        "turnover",
        "total",
        "sum",
        "settlement",
        "subscription",
        "gift",
    )
    if primary is None and amounts:
        primary = amounts[0]

    parsed = {
        "doc_id": doc["doc_id"],
        "type": doc.get("type"),
        "title": doc.get("title") or "",
        "dated": doc.get("dated"),
        "labels": labels,
        "amounts": amounts,
        "primary_amount": primary,
        "accounts": extract_accounts(text),
        "text": text,
        "payee": label_value(
            labels, "pay to", "supplier", "lender", "seller", "from", "client"
        ),
        "payer": label_value(
            labels, "bill to", "borrower", "buyer", "customer", "account holder"
        ),
        "company": label_value(labels, "company", "client", "supplier"),
        "company_number": label_value(labels, "company number"),
        "account_holder": label_value(labels, "account holder", "customer", "borrower"),
    }
    return parsed


def parse_all(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [parse_document(d) for d in docs]


# Document type families used by matchers.
INVOICE_TYPES = {
    "invoice",
    "dealer_invoice",
    "hospital_invoice",
    "supplier_invoice",
    "supplier_invoices",
    "carrier_invoice",
    "freight_invoices",
    "consulting_contract",
    "supply_contract",
    "contract_schedule",
    "purchase_order",
    "payment_file",
    "receipt",
    "sale_receipt",
    "holding_deposit_receipt",
    "remittance_advice",
    "credit_note",
}

OWN_ACCOUNT_TYPES = {"bank_statement", "cancellation_confirmation"}

EVENT_TYPES = {
    "solicitor_letter",
    "insurer_letter",
    "insurance_schedule",
    "vehicle_sale_receipt",
    "loan_agreement",
    "booking_confirmation",
    "venue_email",
    "gift_letter",
    "completion_statement",
    "redundancy_letter",
    "employment_contract",
    "settlement_letter",
    "deposit_return_letter",
    "customer_letter",
    "customer_email",
    "advert",
}

COLLECTION_TYPES = {
    "cash_collection_reports",
    "cash_reconciliation",
    "ticket_report",
    "takings_book",
    "management_accounts",
    "cash_handling_procedure",
}

GROUP_TYPES = {
    "company_register_extract",
    "significant_control_register",
    "group_structure",
    "factoring_agreement",
}

FRAUD_TYPES = {"sending_bank_fraud_report"}

MULE_CHAT_HINTS = (
    "friends sent",
    "invest for them",
    "someone else's",
    "not my money",
    "pass it on",
    "sending it on",
    "mule",
)
