"""Deterministic evidence matchers for each alert / case pattern."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evidence_parse import (
    COLLECTION_TYPES,
    EVENT_TYPES,
    FRAUD_TYPES,
    GROUP_TYPES,
    INVOICE_TYPES,
    MULE_CHAT_HINTS,
    OWN_ACCOUNT_TYPES,
    days_between,
    names_overlap,
    parse_all,
)
from policy import InstitutionPolicy, declared_baseline_gbp


@dataclass
class Disposition:
    verdict: str
    reason_code: str
    evidence_ids: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    weight: int = 0  # higher = more specific / preferred within same verdict


def _uniq(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _tx_amounts(txs: list[dict[str, Any]]) -> list[float]:
    return [float(t.get("amount_gbp") or 0.0) for t in txs]


def _amount_close(a: float, b: float, tol: float = 1.0) -> bool:
    return abs(a - b) <= tol or (b and abs(a - b) / max(abs(b), 1.0) <= 0.01)


def _alert_txs(bundle: dict[str, Any], alert: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {t["transaction_id"]: t for t in bundle["transactions"]}
    # Also search customer transactions for held payments etc.
    for t in bundle["customer_transactions"]:
        by_id.setdefault(t["transaction_id"], t)
    return [by_id[tid] for tid in alert.get("transaction_ids") or [] if tid in by_id]


def _docs_of(parsed: list[dict[str, Any]], types: set[str]) -> list[dict[str, Any]]:
    return [d for d in parsed if d.get("type") in types]


def _text_has_mule_hint(text: str) -> bool:
    low = (text or "").lower()
    return any(h in low for h in MULE_CHAT_HINTS)


def check_fraud_reports(parsed: list[dict[str, Any]], alert_txs: list[dict[str, Any]]) -> Disposition | None:
    for doc in _docs_of(parsed, FRAUD_TYPES):
        text = doc["text"]
        # Match if report amount appears in alert txs or counterparty name overlaps.
        report_amt = doc.get("primary_amount")
        matched = False
        for tx in alert_txs:
            if report_amt is not None and _amount_close(float(tx["amount_gbp"]), report_amt, tol=5.0):
                matched = True
            if names_overlap(tx.get("counterparty_name"), text):
                matched = True
        if not matched and alert_txs:
            # Still escalate if a fraud report sits on the customer with overlapping dates.
            for tx in alert_txs:
                gap = days_between(doc.get("dated"), tx.get("booked_at"))
                if gap is not None and gap <= 14:
                    matched = True
                    break
        if matched or not alert_txs:
            return Disposition(
                "ESCALATE",
                "ESC_ACCOUNT_MISUSE",
                [doc["doc_id"]],
                [
                    f"Sending-bank fraud report {doc['doc_id']} is on file for activity "
                    f"in this review window."
                ],
                weight=100,
            )
    return None


def match_invoice(
    parsed: list[dict[str, Any]],
    alert_txs: list[dict[str, Any]],
    customer: dict[str, Any],
) -> Disposition | None:
    invoices = _docs_of(parsed, INVOICE_TYPES)
    if not invoices or not alert_txs:
        return None

    best: Disposition | None = None
    for doc in invoices:
        amounts = list(doc.get("amounts") or [])
        primary = doc.get("primary_amount")
        if primary is not None and primary not in amounts:
            amounts.append(primary)
        if not amounts:
            continue

        # Optional counterparty alignment against supplier/payee labels or doc text.
        supplier = doc.get("payee") or doc.get("company")
        matched_txs: list[dict[str, Any]] = []
        matched_amts: list[float] = []
        used_amt_idx: set[int] = set()
        for tx in alert_txs:
            tx_amt = float(tx["amount_gbp"])
            gap = days_between(doc.get("dated"), tx.get("booked_at"))
            if gap is not None and gap > 60:
                continue
            if supplier and not (
                names_overlap(supplier, tx.get("counterparty_name"))
                or names_overlap(tx.get("counterparty_name"), doc["text"])
            ):
                # Still allow amount-only match when supplier label absent/unparseable.
                if supplier and doc.get("type") in {
                    "invoice",
                    "supplier_invoice",
                    "supplier_invoices",
                    "consulting_contract",
                    "contract_schedule",
                    "supply_contract",
                    "payment_file",
                }:
                    continue
            found = None
            for i, amt in enumerate(amounts):
                if i in used_amt_idx:
                    continue
                if _amount_close(tx_amt, amt, tol=1.0):
                    found = i
                    break
            if found is None:
                continue
            used_amt_idx.add(found)
            matched_txs.append(tx)
            matched_amts.append(amounts[found])

        if not matched_txs:
            # Clear name mismatch on a single comparable amount → possible invoice mismatch.
            if (
                len(alert_txs) == 1
                and supplier
                and primary is not None
                and _amount_close(float(alert_txs[0]["amount_gbp"]), primary, tol=1.0)
                and not names_overlap(supplier, alert_txs[0].get("counterparty_name"))
            ):
                tx = alert_txs[0]
                mismatch = Disposition(
                    "ESCALATE",
                    "ESC_INVOICE_MISMATCH",
                    [doc["doc_id"], tx["transaction_id"]],
                    [
                        f"Document {doc['doc_id']} for £{primary:,.2f} names {supplier}, "
                        f"which does not match counterparty {tx.get('counterparty_name')}."
                    ],
                    weight=80,
                )
                if best is None or mismatch.weight > best.weight:
                    best = mismatch
            continue

        # Require most alerted txs to be covered when several share the alert.
        if len(alert_txs) >= 3 and len(matched_txs) < max(2, len(alert_txs) - 1):
            continue
        if len(alert_txs) == 2 and len(matched_txs) < 2:
            # Allow 1/2 only when the matched leg is the dominant amount.
            if matched_amts[0] < 0.7 * sum(float(t["amount_gbp"]) for t in alert_txs):
                continue

        party = matched_txs[0].get("counterparty_name") or supplier or "the counterparty"
        if len(matched_txs) == 1:
            tx = matched_txs[0]
            fact = (
                f"{doc['doc_id']} ({doc.get('type')}) for £{matched_amts[0]:,.2f} matches "
                f"transaction {tx['transaction_id']} of £{float(tx['amount_gbp']):,.2f} "
                f"with {party}."
            )
        else:
            total = sum(matched_amts)
            fact = (
                f"{doc['doc_id']} ({doc.get('type')}) schedules {len(matched_txs)} payments "
                f"totalling £{total:,.2f} that match the alerted transactions with {party}."
            )
        disp = Disposition(
            "CLOSE",
            "INVOICE_MATCHES",
            [doc["doc_id"]] + [t["transaction_id"] for t in matched_txs],
            [fact],
            weight=90 + min(5, len(matched_txs)),
        )
        if best is None or (best.verdict != "CLOSE") or disp.weight > best.weight:
            best = disp
            # Strong multi-match can return immediately.
            if len(matched_txs) >= max(1, len(alert_txs) - 1):
                return best
    return best


def match_own_account(
    parsed: list[dict[str, Any]],
    alert_txs: list[dict[str, Any]],
    customer: dict[str, Any],
    policy: InstitutionPolicy,
) -> Disposition | None:
    baseline = declared_baseline_gbp(customer, policy)
    total = sum(float(t["amount_gbp"]) for t in alert_txs)
    cust_name = customer.get("name")

    # Same-name transfers only count as own-account when a document shows the customer holds it.
    same_name = [
        t for t in alert_txs if names_overlap(t.get("counterparty_name"), cust_name)
    ]
    support_docs = _docs_of(parsed, OWN_ACCOUNT_TYPES | GROUP_TYPES)
    support = None
    for doc in support_docs:
        if names_overlap(cust_name, doc.get("account_holder") or "") or names_overlap(
            cust_name, doc["text"]
        ):
            # Statement/register must also reference the counterparty account name or number.
            for tx in same_name:
                cp = tx.get("counterparty_name")
                acct = str(tx.get("counterparty_account") or "")
                if names_overlap(cp, doc["text"]) or (
                    acct and acct.replace(" ", "") in doc["text"].replace(" ", "")
                ):
                    support = doc
                    break
            if support:
                break
            # Business customer: own company register extract is enough with same-name txs.
            if (
                customer.get("kind") == "business"
                and doc["type"] in GROUP_TYPES
                and names_overlap(cust_name, doc["text"])
                and same_name
            ):
                support = doc
                break
    if same_name and support:
        ids = [support["doc_id"]] + [t["transaction_id"] for t in same_name]
        facts = [
            f"{support['doc_id']} shows {cust_name} holds the counterparty account used in "
            f"{same_name[0]['transaction_id']} (£{float(same_name[0]['amount_gbp']):,.2f})."
        ]
        same_total = sum(float(t["amount_gbp"]) for t in same_name)
        if baseline > 0 and same_total >= policy.own_account_rfi_multiple * baseline:
            return Disposition(
                "RFI",
                "RFI_UPDATED_PROFILE",
                _uniq(ids),
                facts
                + [
                    f"Own-account activity of £{same_total:,.2f} is at least "
                    f"{policy.own_account_rfi_multiple:.0f}× the declared baseline "
                    f"of £{baseline:,.2f}; ask for an updated expected income or volume."
                ],
                weight=84,
            )
        return Disposition(
            "CLOSE",
            "OWN_ACCOUNT",
            _uniq(ids),
            facts,
            weight=87,
        )

    stmts = _docs_of(parsed, OWN_ACCOUNT_TYPES)
    if not stmts or not alert_txs:
        return None
    for doc in stmts:
        holder = doc.get("account_holder") or ""
        if holder and not names_overlap(holder, customer.get("name")):
            # Cancellation confirmations often address "Dear Customer" — still OK if accounts match.
            if "account holder" in (doc.get("labels") or {}) and not names_overlap(
                holder, customer.get("name")
            ):
                continue
        for tx in alert_txs:
            acct = tx.get("counterparty_account")
            name_ok = names_overlap(tx.get("counterparty_name"), customer.get("name"))
            acct_ok = False
            if acct:
                for a in doc.get("accounts") or []:
                    if acct.replace(" ", "") in a.replace(" ", "") or a.replace(" ", "") in str(acct).replace(
                        " ", ""
                    ):
                        acct_ok = True
            text_ok = names_overlap(customer.get("name"), doc["text"]) or "in your name" in doc[
                "text"
            ].lower()
            if not (name_ok or acct_ok or text_ok):
                continue
            ids = [doc["doc_id"], tx["transaction_id"]]
            facts = [
                f"Document {doc['doc_id']} shows an account held by {customer.get('name')}, "
                f"matching transfer {tx['transaction_id']} of £{float(tx['amount_gbp']):,.2f}."
            ]
            if baseline > 0 and total >= policy.own_account_rfi_multiple * baseline:
                return Disposition(
                    "RFI",
                    "RFI_UPDATED_PROFILE",
                    ids,
                    facts
                    + [
                        f"Own-account activity of £{total:,.2f} is at least "
                        f"{policy.own_account_rfi_multiple:.0f}× the declared baseline "
                        f"of £{baseline:,.2f}; ask for an updated expected income or volume."
                    ],
                    weight=85,
                )
            return Disposition(
                "CLOSE",
                "OWN_ACCOUNT",
                ids,
                facts,
                weight=88,
            )
    return None


def match_group_company(
    parsed: list[dict[str, Any]],
    alert_txs: list[dict[str, Any]],
    customer: dict[str, Any],
) -> Disposition | None:
    docs = _docs_of(parsed, GROUP_TYPES)
    if not docs or not alert_txs:
        return None
    cust_name = customer.get("name")
    for doc in docs:
        text = doc["text"]
        for tx in alert_txs:
            cp = tx.get("counterparty_name")
            if not cp:
                continue
            # Payment to/from the customer themselves is not a group-company link.
            if names_overlap(cp, cust_name):
                continue
            if not names_overlap(cp, text):
                continue
            # Customer (or their company number) should appear as owner/officer in the doc.
            cust_ok = names_overlap(cust_name, text)
            if customer.get("company_number") and str(customer["company_number"]) in text:
                cust_ok = True
            if not cust_ok:
                continue
            return Disposition(
                "CLOSE",
                "GROUP_COMPANY",
                [doc["doc_id"], tx["transaction_id"]],
                [
                    f"{doc['doc_id']} links counterparty {cp} to the customer's group "
                    f"or common control with {cust_name}."
                ],
                weight=86,
            )
    return None


def match_collection_or_records(
    parsed: list[dict[str, Any]],
    alert_txs: list[dict[str, Any]],
) -> Disposition | None:
    docs = _docs_of(parsed, COLLECTION_TYPES)
    if not docs or not alert_txs:
        return None
    tx_total = sum(float(t["amount_gbp"]) for t in alert_txs)
    for doc in docs:
        amounts = list(doc.get("amounts") or [])
        if not amounts:
            continue
        doc_sum = sum(amounts)
        primary = doc.get("primary_amount")
        pool = list(amounts)
        if primary is not None:
            pool.append(primary)

        matched_txs: list[dict[str, Any]] = []
        matched_amts: list[float] = []
        used: set[int] = set()
        for tx in alert_txs:
            tx_amt = float(tx["amount_gbp"])
            for i, cand in enumerate(pool):
                if i in used:
                    continue
                if _amount_close(tx_amt, cand, tol=5.0):
                    used.add(i)
                    matched_txs.append(tx)
                    matched_amts.append(cand)
                    break

        code = (
            "COLLECTION_DOCUMENTED"
            if doc["type"] in {"cash_collection_reports", "ticket_report"}
            else "BUSINESS_RECORDS_RECONCILE"
        )

        if matched_txs:
            if len(alert_txs) >= 3 and len(matched_txs) < max(2, len(alert_txs) - 1):
                # Too thin for a multi-deposit structuring/cash alert.
                pass
            else:
                if len(matched_txs) == 1 and len(alert_txs) == 1:
                    tx = matched_txs[0]
                    return Disposition(
                        "CLOSE",
                        code,
                        [doc["doc_id"], tx["transaction_id"]],
                        [
                            f"{doc['doc_id']} records £{matched_amts[0]:,.2f}, matching "
                            f"transaction {tx['transaction_id']} of £{float(tx['amount_gbp']):,.2f}."
                        ],
                        weight=87,
                    )
                if len(matched_txs) >= max(1, len(alert_txs) - 1) or (
                    len(matched_txs) >= 2 and sum(matched_amts) >= 0.8 * tx_total
                ):
                    return Disposition(
                        "CLOSE",
                        code,
                        [doc["doc_id"]] + [t["transaction_id"] for t in matched_txs],
                        [
                            f"{doc['doc_id']} records {len(matched_txs)} cash/takings lines "
                            f"totalling £{sum(matched_amts):,.2f} that reconcile to the "
                            f"alerted deposits."
                        ],
                        weight=88,
                    )

        # Approximate total reconciliation for multi-deposit cases.
        for cand in [doc_sum, primary]:
            if cand is None:
                continue
            if _amount_close(tx_total, cand, tol=25.0):
                return Disposition(
                    "CLOSE",
                    "BUSINESS_RECORDS_RECONCILE",
                    [doc["doc_id"]] + [t["transaction_id"] for t in alert_txs],
                    [
                        f"{doc['doc_id']} totals about £{cand:,.2f}, reconciling to "
                        f"£{tx_total:,.2f} across the alerted transactions."
                    ],
                    weight=84,
                )
    return None


def match_documented_event(
    parsed: list[dict[str, Any]],
    alert_txs: list[dict[str, Any]],
    customer: dict[str, Any],
) -> Disposition | None:
    docs = _docs_of(parsed, EVENT_TYPES)
    if not docs or not alert_txs:
        return None
    for doc in docs:
        amt = doc.get("primary_amount")
        if amt is None:
            continue
        for tx in alert_txs:
            if not _amount_close(float(tx["amount_gbp"]), amt, tol=2.0):
                continue
            gap = days_between(doc.get("dated"), tx.get("booked_at"))
            if gap is not None and gap > 60:
                continue
            text_l = doc["text"].lower()
            if doc["type"] == "gift_letter":
                code = "THIRD_PARTY_DOCUMENTED"
            elif doc["type"] in {
                "deposit_return_letter",
                "credit_note",
                "cancellation_confirmation",
            } or "refund" in text_l:
                code = "REFUND_OF_EARLIER_PAYMENT"
            else:
                code = "DOCUMENTED_EVENT"
            who = tx.get("counterparty_name") or doc.get("payee") or "the counterparty"
            evidence = [doc["doc_id"], tx["transaction_id"]]
            facts = [
                f"{doc['doc_id']} documents £{amt:,.2f} involving {who}, matching "
                f"transaction {tx['transaction_id']} of £{float(tx['amount_gbp']):,.2f}."
            ]
            # Loan for a vehicle: also attach a nearby outbound to a motor/dealer payee.
            if doc["type"] == "loan_agreement" and any(
                w in text_l for w in ("car", "vehicle", "motor")
            ):
                for out in alert_txs:
                    if out.get("direction") != "out":
                        continue
                    cp = (out.get("counterparty_name") or "").lower()
                    if any(w in cp for w in ("motor", "car", "auto", "garage", "dealer", "vehicle")):
                        evidence.append(out["transaction_id"])
                        facts.append(
                            f"Loan purpose is a vehicle purchase, matching outbound "
                            f"{out['transaction_id']} of £{float(out['amount_gbp']):,.2f} to "
                            f"{out.get('counterparty_name')}."
                        )
                        break
            return Disposition(
                "CLOSE",
                code,
                _uniq(evidence),
                facts,
                weight=85,
            )
    return None


def match_refund(
    parsed: list[dict[str, Any]],
    alert_txs: list[dict[str, Any]],
    customer_txs: list[dict[str, Any]],
) -> Disposition | None:
    for tx in alert_txs:
        if tx.get("direction") != "in":
            continue
        amt = float(tx["amount_gbp"])
        cp = tx.get("counterparty_name")
        for prior in customer_txs:
            if prior["transaction_id"] == tx["transaction_id"]:
                continue
            if prior.get("direction") != "out":
                continue
            if not _amount_close(float(prior["amount_gbp"]), amt, tol=1.0):
                continue
            if cp and prior.get("counterparty_name") and not names_overlap(cp, prior.get("counterparty_name")):
                continue
            # prior should be before this tx
            if (prior.get("booked_at") or "") >= (tx.get("booked_at") or ""):
                continue
            return Disposition(
                "CLOSE",
                "REFUND_OF_EARLIER_PAYMENT",
                [tx["transaction_id"], prior["transaction_id"]],
                [
                    f"Incoming {tx['transaction_id']} of £{amt:,.2f} from {cp} refunds "
                    f"earlier payment {prior['transaction_id']} of "
                    f"£{float(prior['amount_gbp']):,.2f}."
                ],
                weight=82,
            )
    # Explicit refund-like docs already handled in match_documented_event.
    return None


def match_list(
    alert: dict[str, Any],
    parsed: list[dict[str, Any]],
    alert_txs: list[dict[str, Any]],
    internal_list: dict[str, Any],
    customer: dict[str, Any],
) -> Disposition | None:
    measured = alert.get("measured") or {}
    entry_id = measured.get("list_entry")
    if not entry_id:
        return Disposition(
            "RFI",
            "RFI_COUNTERPARTY_DETAILS",
            [],
            [
                "Internal list match is unsettled on the file; ask who the counterparty "
                "is and for documents that confirm they are not the listed party."
            ],
            weight=40,
        )
    entry = internal_list.get(entry_id) or {}
    matched_on = (measured.get("matched_on") or "").lower()
    matched_party = measured.get("matched_party") or ""
    list_name = entry.get("name") or ""
    list_dob = entry.get("date_of_birth") or ""
    facts = [
        f"Alert matched internal list entry {entry_id} on {measured.get('matched_on')}: "
        f"{matched_party}."
    ]
    tx_ids = [t["transaction_id"] for t in alert_txs[:1]]

    strong = False
    if ("date of birth" in matched_on or "dob" in matched_on) and "name" in matched_on:
        if "month and year" not in matched_on:
            strong = True
    if "identifier" in matched_on or "internal case" in matched_on:
        strong = True

    # Customer themselves colliding with a listed name but different DOB.
    cust_dob = customer.get("date_of_birth") or ""
    if (
        entry.get("kind") == "individual"
        and cust_dob
        and list_dob
        and cust_dob != list_dob
        and names_overlap(customer.get("name"), list_name)
    ):
        kyc_ids = [d["doc_id"] for d in parsed if d["type"] == "due_diligence_summary"][:1]
        return Disposition(
            "CLOSE",
            "LIST_MATCH_DIFFERENT_PARTY",
            _uniq([entry_id] + kyc_ids),
            facts
            + [
                f"Customer date of birth {cust_dob} differs from listed party "
                f"{list_name} ({list_dob})."
            ],
            weight=91,
        )

    registers = _docs_of(parsed, GROUP_TYPES)
    for doc in registers:
        text = doc["text"]
        # Register for the matched company/counterparty.
        about_counterparty = names_overlap(matched_party, text) or any(
            names_overlap(t.get("counterparty_name"), text) for t in alert_txs
        )
        if not about_counterparty:
            continue

        listed_on_register = names_overlap(list_name, text)
        if listed_on_register:
            # Same person appears on the company record.
            if list_dob:
                years = set(re_years(text))
                # Explicit different birth year next to the listed name → different party.
                # Heuristic: if year present elsewhere but a nearby officer line has another year.
                if list_dob[:4] in years:
                    return Disposition(
                        "ESCALATE",
                        "ESC_LISTED_PARTY",
                        _uniq([entry_id, doc["doc_id"]] + tx_ids),
                        facts
                        + [
                            f"{doc['doc_id']} shows {list_name} linked to the counterparty "
                            f"with birth details consistent with list entry {entry_id}."
                        ],
                        weight=96,
                    )
                # Name on register but DOB year absent / conflicting → check ceased wording.
                if "until" in matched_party.lower() or "until" in text.lower():
                    return Disposition(
                        "RFI",
                        "RFI_COUNTERPARTY_DETAILS",
                        _uniq([entry_id, doc["doc_id"]] + tx_ids),
                        facts
                        + [
                            f"{doc['doc_id']} mentions {list_name}; ask for documents confirming "
                            f"whether they still control the counterparty."
                        ],
                        weight=70,
                    )
                return Disposition(
                    "ESCALATE",
                    "ESC_LISTED_PARTY",
                    _uniq([entry_id, doc["doc_id"]] + tx_ids),
                    facts
                    + [
                        f"{doc['doc_id']} links listed person {list_name} to the counterparty."
                    ],
                    weight=94,
                )

            return Disposition(
                "ESCALATE",
                "ESC_LISTED_PARTY",
                _uniq([entry_id, doc["doc_id"]] + tx_ids),
                facts
                + [
                    f"{doc['doc_id']} links listed party {list_name} to the counterparty."
                ],
                weight=94,
            )

        # Counterparty company record exists and the listed individual is not on it.
        if entry.get("kind") == "individual" and (
            "officer" in text.lower()
            or "director" in text.lower()
            or "significant control" in text.lower()
        ):
            return Disposition(
                "CLOSE",
                "LIST_MATCH_DIFFERENT_PARTY",
                _uniq([entry_id, doc["doc_id"]] + tx_ids),
                facts
                + [
                    f"{doc['doc_id']} is the company record for the counterparty and does not "
                    f"list {list_name} (DOB {list_dob or 'n/a'}) as an officer or PSC, so this "
                    f"is not the listed party."
                ],
                weight=92,
            )

        # Business list entry vs different company on register.
        if entry.get("kind") == "business":
            entry_co = entry.get("identifiers") or {}
            doc_co = doc.get("company_number")
            if doc_co and entry_co.get("company_number") and str(doc_co) != str(
                entry_co.get("company_number")
            ):
                return Disposition(
                    "CLOSE",
                    "LIST_MATCH_DIFFERENT_PARTY",
                    _uniq([entry_id, doc["doc_id"]] + tx_ids),
                    facts
                    + [
                        f"{doc['doc_id']} has company number {doc_co}, which differs from "
                        f"listed business {list_name}."
                    ],
                    weight=93,
                )

    # Name + month/year of birth on an officer without register exoneration.
    if "month and year" in matched_on and "name" in matched_on:
        return Disposition(
            "RFI",
            "RFI_COUNTERPARTY_DETAILS",
            _uniq([entry_id] + tx_ids),
            facts
            + [
                "Ask for documents confirming the counterparty officer is not the listed "
                "person (match is name plus month/year of birth only)."
            ],
            weight=60,
        )

    if strong:
        return Disposition(
            "ESCALATE",
            "ESC_LISTED_PARTY",
            _uniq([entry_id] + [t["transaction_id"] for t in alert_txs[:2]]),
            facts
            + [
                "The match includes strong identity details, so treat the counterparty as "
                "the listed party or controlled by it."
            ],
            weight=95,
        )

    # Address-only / name-only / unsettled.
    return Disposition(
        "RFI",
        "RFI_COUNTERPARTY_DETAILS",
        _uniq([entry_id] + tx_ids),
        facts
        + [
            "Ask the customer who the counterparty is and for documents confirming they "
            "are not the listed party (the match is on limited details only)."
        ],
        weight=55,
    )


def re_years(text: str) -> list[str]:
    import re

    return re.findall(r"\b(19\d{2}|20\d{2})\b", text or "")


def match_returning_customer(
    alert: dict[str, Any],
    parsed: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    alert_txs: list[dict[str, Any]],
) -> Disposition | None:
    if alert.get("rule_id") != "TM07":
        return None
    # Prefer explicit reverification / KYC + dormancy explanation docs.
    useful = [
        d
        for d in parsed
        if d["type"]
        in {
            "reverification_record",
            "due_diligence_summary",
            "contact_note",
            "support_chat",
            "customer_email",
            "customer_letter",
        }
    ]
    # If a prior review closed dormancy-like activity, only reuse if note covers this.
    for rev in reviews:
        note = (rev.get("note") or "").lower()
        if rev.get("verdict") == "CLOSE" and any(
            w in note for w in ("dormant", "reactiv", "returned", "resume", "back")
        ):
            ids = [rev.get("review_id")] + [t["transaction_id"] for t in alert_txs[:1]]
            return Disposition(
                "CLOSE",
                "RETURNING_CUSTOMER",
                _uniq([i for i in ids if i]),
                [
                    f"Earlier review {rev.get('review_id')} closed similar reactivation: "
                    f"{rev.get('note')}"
                ],
                weight=70,
            )
    # Documented event already may explain TM07; if reverification recently done, soft close.
    for doc in useful:
        if doc["type"] == "reverification_record":
            return Disposition(
                "CLOSE",
                "RETURNING_CUSTOMER",
                [doc["doc_id"]] + [t["transaction_id"] for t in alert_txs[:1]],
                [
                    f"Identity re-verification {doc['doc_id']} supports that the verified "
                    f"customer resumed use of the dormant account."
                ],
                weight=72,
            )
    return None


def match_within_profile(
    alert: dict[str, Any],
    customer: dict[str, Any],
    policy: InstitutionPolicy,
    alert_txs: list[dict[str, Any]],
    parsed: list[dict[str, Any]],
) -> Disposition | None:
    if alert.get("rule_id") != "TM01":
        return None
    measured = alert.get("measured") or {}
    multiple = measured.get("multiple")
    baseline = declared_baseline_gbp(customer, policy)
    # Only close as within-profile when a free-text contact/staff note explicitly says so.
    # Do not use KYC templates — they always mention salary / expected channels.
    for doc in parsed:
        if doc["type"] not in {"contact_note", "support_chat", "staff_note"}:
            continue
        text = doc["text"].lower()
        if any(
            p in text
            for p in (
                "within expected",
                "as expected",
                "consistent with declared",
                "in line with expected",
                "expected activity",
                "normal pattern",
                "fits their profile",
                "within their profile",
            )
        ):
            return Disposition(
                "CLOSE",
                "WITHIN_PROFILE",
                [doc["doc_id"]] + [t["transaction_id"] for t in alert_txs[:2]],
                [
                    f"Note {doc['doc_id']} confirms the activity fits the customer's "
                    f"declared profile (baseline £{baseline:,.2f}"
                    + (f", measured multiple {multiple})" if multiple is not None else ")")
                ],
                weight=60,
            )
    return None


def match_early_redemption(
    parsed: list[dict[str, Any]],
    alert_txs: list[dict[str, Any]],
    customer: dict[str, Any],
) -> Disposition | None:
    """Investment TM10: cancellation or documented personal need + own-account payout."""
    for doc in parsed:
        if doc["type"] == "cancellation_confirmation":
            amt = doc.get("primary_amount")
            ids = [doc["doc_id"]]
            facts = [
                f"Cancellation confirmation {doc['doc_id']} explains the early return of "
                f"subscribed funds"
                + (f" (£{amt:,.2f})" if amt is not None else "")
                + "."
            ]
            for tx in alert_txs:
                if amt is not None and _amount_close(float(tx["amount_gbp"]), amt, tol=2.0):
                    ids.append(tx["transaction_id"])
            return Disposition("CLOSE", "DOCUMENTED_EVENT", _uniq(ids), facts, weight=88)

    need_docs = [
        d
        for d in parsed
        if d["type"]
        in {
            "redundancy_letter",
            "customer_email",
            "customer_letter",
            "completion_statement",
            "insurer_letter",
        }
    ]
    stmts = _docs_of(parsed, OWN_ACCOUNT_TYPES)
    strong_need = False
    need_ids: list[str] = []
    need_facts: list[str] = []
    for doc in need_docs:
        text = doc["text"].lower()
        personal = any(
            w in text
            for w in (
                "redundant",
                "redundancy",
                "living expenses",
                "made redundant",
                "completion",
                "sale of",
                "property sold",
                "critical illness",
            )
        )
        if not personal:
            continue
        strong_need = True
        need_ids.append(doc["doc_id"])
        need_facts.append(
            f"{doc['doc_id']} documents a legitimate personal reason for selling fund units early."
        )
        amt = doc.get("primary_amount")
        if amt is not None:
            for tx in alert_txs:
                if _amount_close(float(tx["amount_gbp"]), amt, tol=2.0):
                    need_ids.append(tx["transaction_id"])
                    return Disposition(
                        "CLOSE",
                        "DOCUMENTED_EVENT",
                        _uniq(need_ids),
                        need_facts
                        + [
                            f"Amount £{amt:,.2f} matches alerted movement {tx['transaction_id']}."
                        ],
                        weight=87,
                    )

    if strong_need:
        for stmt in stmts:
            if names_overlap(customer.get("name"), stmt.get("account_holder") or stmt["text"]):
                need_ids.append(stmt["doc_id"])
                need_facts.append(
                    f"Withdrawal destination is supported by own-account document {stmt['doc_id']}."
                )
                break
        # Cite redemption/subscription legs when present.
        for tx in alert_txs:
            ch = (tx.get("channel") or "").lower()
            if "fund" in ch or "redempt" in ch or "subscript" in ch:
                need_ids.append(tx["transaction_id"])
        return Disposition(
            "CLOSE",
            "DOCUMENTED_EVENT",
            _uniq(need_ids + [t["transaction_id"] for t in alert_txs[:2]]),
            need_facts,
            weight=86,
        )
    return None


def match_investment_third_party(
    parsed: list[dict[str, Any]],
    alert_txs: list[dict[str, Any]],
    customer: dict[str, Any],
) -> Disposition | None:
    """Extra TM08 explainers: employment remittance, completion funds, insurer payouts."""
    for doc in parsed:
        if doc["type"] not in {
            "employment_contract",
            "completion_statement",
            "insurer_letter",
            "gift_letter",
            "redundancy_letter",
        }:
            continue
        amt = doc.get("primary_amount")
        if amt is None:
            continue
        for tx in alert_txs:
            if not _amount_close(float(tx["amount_gbp"]), amt, tol=2.0):
                continue
            # Counterparty should appear in the document when present.
            if tx.get("counterparty_name") and not names_overlap(
                tx.get("counterparty_name"), doc["text"]
            ):
                # employment/insurer may pay from company name not in short extract
                if doc["type"] not in {"employment_contract", "insurer_letter", "completion_statement"}:
                    continue
            code = (
                "THIRD_PARTY_DOCUMENTED"
                if doc["type"] in {"gift_letter", "employment_contract"}
                else "DOCUMENTED_EVENT"
            )
            return Disposition(
                "CLOSE",
                code,
                [doc["doc_id"], tx["transaction_id"]],
                [
                    f"{doc['doc_id']} ({doc['type']}) for £{amt:,.2f} explains third-party "
                    f"or documented funding on {tx['transaction_id']}."
                ],
                weight=87,
            )
    return None


def suspicion_from_chats(parsed: list[dict[str, Any]]) -> Disposition | None:
    for doc in parsed:
        if doc["type"] not in {"support_chat", "contact_note", "staff_note", "customer_email"}:
            continue
        if _text_has_mule_hint(doc["text"]):
            return Disposition(
                "ESCALATE",
                "ESC_MULE_ACTIVITY",
                [doc["doc_id"]],
                [
                    f"{doc['doc_id']} indicates the customer is moving other people's money "
                    f"through the account."
                ],
                weight=98,
            )
    return None


def explain_alert(
    bundle: dict[str, Any],
    alert: dict[str, Any],
    parsed: list[dict[str, Any]],
    policy: InstitutionPolicy,
) -> Disposition:
    alert_txs = _alert_txs(bundle, alert)
    customer = bundle["customer"]
    reviews = bundle["previous_reviews"]

    # Hard suspicion first.
    fraud = check_fraud_reports(parsed, alert_txs)
    if fraud:
        return fraud
    mule = suspicion_from_chats(parsed)
    if mule:
        return mule

    rule = alert.get("rule_id")

    if rule == "TM22":
        return match_list(
            alert, parsed, alert_txs, bundle["internal_list"], customer
        ) or Disposition(
            "RFI",
            "RFI_COUNTERPARTY_DETAILS",
            [],
            ["Ask for documents confirming the counterparty is not the listed party."],
            30,
        )

    # Positive CLOSE explainers.
    candidates: list[Disposition] = []
    for fn in (
        lambda: match_invoice(parsed, alert_txs, customer),
        lambda: match_own_account(parsed, alert_txs, customer, policy),
        lambda: match_group_company(parsed, alert_txs, customer),
        lambda: match_collection_or_records(parsed, alert_txs),
        lambda: match_documented_event(parsed, alert_txs, customer),
        lambda: match_investment_third_party(parsed, alert_txs, customer),
        lambda: match_refund(parsed, alert_txs, bundle["customer_transactions"]),
        lambda: match_returning_customer(alert, parsed, reviews, alert_txs),
        lambda: match_within_profile(alert, customer, policy, alert_txs, parsed),
        lambda: match_early_redemption(parsed, alert_txs, customer)
        if rule == "TM10"
        else None,
    ):
        disp = fn()
        if disp:
            candidates.append(disp)

    # Prefer escalate/mismatch over close if present; else best close; else rfi.
    escalations = [c for c in candidates if c.verdict == "ESCALATE"]
    if escalations:
        return max(escalations, key=lambda c: c.weight)
    closes = [c for c in candidates if c.verdict == "CLOSE"]
    rfis = [c for c in candidates if c.verdict == "RFI"]
    closes = [c for c in closes if _close_covers_alert(rule, alert, c, alert_txs)]
    if closes:
        return max(closes, key=lambda c: c.weight)
    if rfis:
        return max(rfis, key=lambda c: c.weight)

    return default_for_rule(rule, alert, alert_txs, parsed, policy, customer, bundle)


def _close_covers_alert(
    rule: str | None,
    alert: dict[str, Any],
    close: Disposition,
    alert_txs: list[dict[str, Any]],
) -> bool:
    """Reject thin CLOSE explanations that leave material alert activity unexplained."""
    measured = alert.get("measured") or {}
    cited = set(close.evidence_ids)

    if rule == "TM02":
        # Rapid movement needs the pass-through explained, not only one inbound credit.
        ins = [t for t in alert_txs if t.get("direction") == "in"]
        outs = [t for t in alert_txs if t.get("direction") == "out"]
        if close.reason_code == "OWN_ACCOUNT":
            return True
        cited_ins = [t for t in ins if t["transaction_id"] in cited]
        cited_outs = [t for t in outs if t["transaction_id"] in cited]
        if outs and not cited_outs and close.reason_code not in {
            "REFUND_OF_EARLIER_PAYMENT",
            "INVOICE_MATCHES",
        }:
            # Allow loan/event closes only when the narrative clearly funds the outbound purpose.
            joined = " ".join(close.facts).lower()
            if any(w in joined for w in ("car", "vehicle", "purchase", "motor", "invoice", "contract")):
                return bool(cited_ins)
            return False
        if ins and not cited_ins and close.reason_code not in {"OWN_ACCOUNT"}:
            return False
        return True

    if rule == "TM07":
        moved = float(measured.get("moved_gbp") or 0.0)
        if moved <= 0:
            return True
        covered = sum(
            float(t["amount_gbp"]) for t in alert_txs if t["transaction_id"] in cited
        )
        if close.reason_code in {"OWN_ACCOUNT", "RETURNING_CUSTOMER"}:
            # All alerted legs should be own-name/returning, not a thin slice of a larger move.
            return covered >= 0.85 * moved or (
                alert_txs and all(t["transaction_id"] in cited for t in alert_txs)
            )
        return covered >= 0.85 * moved

    if rule == "TM06":
        # Must touch the corridor transactions themselves.
        if any(t["transaction_id"] in cited for t in alert_txs):
            return True
        return close.reason_code in {"OWN_ACCOUNT", "INVOICE_MATCHES", "DOCUMENTED_EVENT", "REFUND_OF_EARLIER_PAYMENT", "GROUP_COMPANY"}

    if rule == "TM08":
        # Third-party alert cannot close as plain OWN_ACCOUNT unless the statement shows the
        # third-party account name (joint account etc.) — matcher already gates that.
        return True

    if rule != "TM01":
        return True

    received = float(measured.get("received_gbp") or measured.get("total_gbp") or 0.0)
    if received <= 0:
        return True
    covered = sum(
        float(t["amount_gbp"])
        for t in alert_txs
        if t["transaction_id"] in cited and t.get("direction") == "in"
    )
    if covered <= 0:
        if close.reason_code in {
            "OWN_ACCOUNT",
            "RETURNING_CUSTOMER",
            "WITHIN_PROFILE",
            "GROUP_COMPANY",
        }:
            return True
        if len(alert_txs) <= 1:
            return True
        return False
    baseline = float(measured.get("baseline_gbp") or 0.0)
    excess = max(0.0, received - baseline)
    return covered >= 0.6 * received or covered >= 0.8 * excess


def default_for_rule(
    rule: str | None,
    alert: dict[str, Any],
    alert_txs: list[dict[str, Any]],
    parsed: list[dict[str, Any]],
    policy: InstitutionPolicy,
    customer: dict[str, Any],
    bundle: dict[str, Any],
) -> Disposition:
    tx_ids = [t["transaction_id"] for t in alert_txs[:4]]
    measured = alert.get("measured") or {}

    if rule == "TM02":
        # Rapid movement without explanation → pass-through / mule-ish.
        distinct = measured.get("distinct_counterparties")
        return Disposition(
            "ESCALATE",
            "ESC_PASS_THROUGH",
            tx_ids,
            [
                "Funds are received and paid out rapidly with no document on file that "
                "explains a legitimate purpose for the pass-through."
            ],
            weight=75,
        )

    if rule == "TM03":
        # Structuring without cash/business records.
        has_records = bool(_docs_of(parsed, COLLECTION_TYPES | INVOICE_TYPES))
        if has_records:
            return Disposition(
                "RFI",
                "RFI_BUSINESS_RECORDS",
                tx_ids + [d["doc_id"] for d in _docs_of(parsed, COLLECTION_TYPES)[:1]],
                [
                    "Repeated near-threshold transactions are on file; ask for the business "
                    "records that break down each payment/deposit."
                ],
                weight=65,
            )
        return Disposition(
            "ESCALATE",
            "ESC_STRUCTURING",
            tx_ids,
            [
                "Repeated transactions sit just below the reporting threshold with no "
                "credible records explaining why they were split."
            ],
            weight=80,
        )

    if rule == "TM04":
        return Disposition(
            "ESCALATE",
            "ESC_MULE_ACTIVITY",
            tx_ids,
            [
                "Many distinct counterparties appear in a short window with no collection "
                "mandate, invoice set, or other explanation on file."
            ],
            weight=78,
        )

    if rule == "TM05":
        amt = measured.get("amount_gbp")
        amt_txt = f" of £{float(amt):,.2f}" if amt is not None else ""
        return Disposition(
            "RFI",
            "RFI_SOURCE_OF_FUNDS",
            tx_ids,
            [
                f"Ask for source-of-funds documents for the large cash activity{amt_txt}."
            ],
            weight=60,
        )

    if rule == "TM06":
        return Disposition(
            "ESCALATE",
            "ESC_HIGH_RISK_CORRIDOR",
            tx_ids,
            [
                "Payments involve an undeclared higher-risk country and nothing on file "
                "explains the corridor activity."
            ],
            weight=80,
        )

    if rule == "TM07":
        return Disposition(
            "RFI",
            "RFI_SOURCE_OF_FUNDS",
            tx_ids,
            [
                "The dormant account moved £5,000 or more; ask what resumed the activity "
                "and for source-of-funds evidence."
            ],
            weight=58,
        )

    if rule == "TM08":
        return Disposition(
            "ESCALATE",
            "ESC_THIRD_PARTY_FUNDING",
            tx_ids,
            [
                "A third-party name appears on funding or payout with no gift letter, "
                "invoice, or other relationship document on file."
            ],
            weight=80,
        )

    if rule == "TM10":
        explained = match_early_redemption(parsed, alert_txs, customer)
        if explained:
            return explained
        return Disposition(
            "ESCALATE",
            "ESC_EARLY_REDEMPTION",
            tx_ids,
            [
                "Fund units were bought and sold back quickly with no satisfactory "
                "explanation that this was ordinary investment activity."
            ],
            weight=82,
        )

    if rule == "TM01":
        # Over profile without docs → RFI for purpose/source, not auto-escalate.
        kind = customer.get("kind")
        if kind == "business" or _docs_of(parsed, COLLECTION_TYPES):
            return Disposition(
                "RFI",
                "RFI_BUSINESS_RECORDS",
                tx_ids,
                [
                    "Receipts exceed the declared profile; ask for business records that "
                    "support the higher volume."
                ],
                weight=55,
            )
        return Disposition(
            "RFI",
            "RFI_SOURCE_OF_FUNDS",
            tx_ids,
            [
                "Receipts exceed the declared expected income/volume; ask for source-of-funds "
                "documents for the excess."
            ],
            weight=55,
        )

    # payments_fx invoice gate for held payments.
    held = bundle.get("held_payment")
    if held and policy.invoice_gate_gbp:
        amt = float(held.get("amount_gbp") or 0)
        cp = held.get("counterparty_name")
        own = names_overlap(cp, customer.get("name"))
        if amt >= policy.invoice_gate_gbp and not own:
            inv = match_invoice(parsed, [held], customer)
            if inv and inv.verdict == "CLOSE":
                return inv
            return Disposition(
                "RFI",
                "RFI_PURPOSE_DOCUMENTS",
                [held["transaction_id"]],
                [
                    f"Held payment {held['transaction_id']} of £{amt:,.2f} to {cp} is "
                    f"£{policy.invoice_gate_gbp:,.0f}+ and has no matching invoice/contract "
                    f"on file; ask for the invoice or contract before release."
                ],
                weight=70,
            )

    return Disposition(
        "ESCALATE",
        "ESC_UNEXPLAINED_FUNDS",
        tx_ids,
        [
            "No document on file credibly explains the alerted activity, so escalate "
            "rather than close."
        ],
        weight=40,
    )


def prepare_parsed(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return parse_all(bundle["evidence"])
