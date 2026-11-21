"""Load institution folder JSONL files and assemble per-case bundles."""

from __future__ import annotations

import json
import os
from typing import Any


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _index_by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {row[key]: row for row in rows}


def _group_by(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row[key], []).append(row)
    return out


class InstitutionData:
    def __init__(self, folder: str):
        self.folder = os.path.abspath(folder)
        self.institution = os.path.basename(self.folder.rstrip(os.sep))

        self.cases = _read_jsonl(os.path.join(self.folder, "cases.jsonl"))
        self.alerts = _index_by(
            _read_jsonl(os.path.join(self.folder, "alerts.jsonl")), "alert_id"
        )
        self.customers = _index_by(
            _read_jsonl(os.path.join(self.folder, "customers.jsonl")), "customer_id"
        )
        self.transactions = _index_by(
            _read_jsonl(os.path.join(self.folder, "transactions.jsonl")),
            "transaction_id",
        )
        evidence_rows = _read_jsonl(os.path.join(self.folder, "evidence.jsonl"))
        self.evidence_meta = _index_by(evidence_rows, "doc_id")
        self.evidence_by_customer = _group_by(evidence_rows, "customer_id")
        self.reviews_by_customer = _group_by(
            _read_jsonl(os.path.join(self.folder, "previous_reviews.jsonl")),
            "customer_id",
        )
        self.internal_list = _index_by(
            _read_jsonl(os.path.join(self.folder, "internal_list.jsonl")), "entry_id"
        )
        self._evidence_text: dict[str, str] = {}

    def evidence_text(self, doc_id: str) -> str:
        if doc_id in self._evidence_text:
            return self._evidence_text[doc_id]
        meta = self.evidence_meta[doc_id]
        path = os.path.join(self.folder, meta["file"])
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self._evidence_text[doc_id] = text
        return text

    def assemble_case(self, case: dict[str, Any]) -> dict[str, Any]:
        customer_id = case["customer_id"]
        alerts = [self.alerts[aid] for aid in case["alert_ids"]]
        tx_ids: list[str] = []
        seen: set[str] = set()
        for alert in alerts:
            for tid in alert.get("transaction_ids") or []:
                if tid not in seen:
                    seen.add(tid)
                    tx_ids.append(tid)
        held_id = case.get("held_payment_id")
        if held_id and held_id not in seen:
            tx_ids.append(held_id)

        transactions = [
            self.transactions[tid] for tid in tx_ids if tid in self.transactions
        ]
        # Also keep a wider customer transaction index for refund / own-account checks.
        customer_txs = [
            tx
            for tx in self.transactions.values()
            if tx.get("customer_id") == customer_id
        ]
        customer_txs.sort(key=lambda t: t.get("booked_at") or "")

        evidence_meta = list(self.evidence_by_customer.get(customer_id, []))
        evidence_docs = []
        for meta in evidence_meta:
            evidence_docs.append(
                {
                    **meta,
                    "text": self.evidence_text(meta["doc_id"]),
                }
            )

        return {
            "case": case,
            "institution": self.institution,
            "customer": self.customers[customer_id],
            "alerts": alerts,
            "transactions": transactions,
            "customer_transactions": customer_txs,
            "evidence": evidence_docs,
            "previous_reviews": list(self.reviews_by_customer.get(customer_id, [])),
            "internal_list": self.internal_list,
            "held_payment": self.transactions.get(held_id) if held_id else None,
        }
