"""Per-customer statistical features for safer triage.

Priors and demoters only — never emit CLOSE from these features alone.
History is taken from the same customer's transactions *before* the review
window so the alerted activity does not explain itself.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from evidence_parse import norm_name, parse_date


_PERIODIC_CHANNELS = frozenset({"bacs", "standing_order", "direct_debit"})
_CASH_CHANNELS = frozenset({"cash_deposit", "cash_withdrawal", "cash_topup"})
_NOVEL_FLOOR_GBP = 1000.0


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = (len(sorted_vals) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _median(vals: list[float]) -> float:
    return _percentile(sorted(vals), 0.5)


def _iqr(vals: list[float]) -> float:
    s = sorted(vals)
    return _percentile(s, 0.75) - _percentile(s, 0.25)


def _parse_booked(value: str | None) -> datetime | None:
    """Parse JSONL booked_at / window dates (ISO date or datetime)."""
    if not value:
        return None
    text = str(value).strip()
    # Full ISO datetime: 2026-05-19T05:30:00Z
    if "T" in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            text = text[:10]
    return parse_date(text)


def _tx_date(tx: dict[str, Any]) -> datetime | None:
    return _parse_booked(tx.get("booked_at"))


def _history_before(
    customer_txs: list[dict[str, Any]], window_from: str | None
) -> list[dict[str, Any]]:
    cutoff = _parse_booked(window_from) if window_from else None
    if not cutoff:
        # No window: use all but the most recent day of activity as history.
        return list(customer_txs)
    out: list[dict[str, Any]] = []
    for tx in customer_txs:
        dt = _tx_date(tx)
        if dt is None:
            continue
        if dt.date() < cutoff.date():
            out.append(tx)
    return out


def _bucket_key(tx: dict[str, Any]) -> tuple[str, str]:
    return (str(tx.get("direction") or ""), str(tx.get("channel") or ""))


def _amount_norms(
    history: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, float]]:
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for tx in history:
        buckets[_bucket_key(tx)].append(float(tx.get("amount_gbp") or 0.0))
    norms: dict[tuple[str, str], dict[str, float]] = {}
    for key, amts in buckets.items():
        if len(amts) < 3:
            continue
        med = _median(amts)
        iqr = _iqr(amts)
        s = sorted(amts)
        p95 = _percentile(s, 0.95)
        high = med + 3.0 * iqr if iqr > 1.0 else p95
        norms[key] = {
            "median": med,
            "iqr": max(iqr, 1.0),
            "high": max(high, med),
            "count": float(len(amts)),
        }
    return norms


def _monthly_inbound_totals(history: list[dict[str, Any]]) -> list[float]:
    by_month: dict[str, float] = defaultdict(float)
    for tx in history:
        if tx.get("direction") != "in":
            continue
        dt = _tx_date(tx)
        if not dt:
            continue
        key = f"{dt.year:04d}-{dt.month:02d}"
        by_month[key] += float(tx.get("amount_gbp") or 0.0)
    return [by_month[k] for k in sorted(by_month.keys())]


def _cp_history(
    history: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_cp: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tx in history:
        cp = norm_name(tx.get("counterparty_name"))
        if not cp:
            continue
        by_cp[cp].append(tx)
    out: dict[str, dict[str, Any]] = {}
    for cp, txs in by_cp.items():
        amts = [float(t.get("amount_gbp") or 0.0) for t in txs]
        dates = [_tx_date(t) for t in txs]
        dates_ok = sorted(d for d in dates if d is not None)
        gaps: list[float] = []
        for i in range(1, len(dates_ok)):
            gaps.append(float((dates_ok[i].date() - dates_ok[i - 1].date()).days))
        periodic = False
        med_gap = 0.0
        regularity = 0.0
        if len(gaps) >= 3:
            med_gap = _median(gaps)
            gap_iqr = _iqr(gaps)
            regularity = 1.0 - (gap_iqr / max(med_gap, 1.0))
            # Regular recurring series: stable gaps from a few days through monthly.
            # (Covers semi-weekly payroll, weekly, fortnightly, and monthly cycles.)
            periodic = regularity >= 0.5 and 2.0 <= med_gap <= 40.0
        out[cp] = {
            "count": len(txs),
            "median": _median(amts) if amts else 0.0,
            "max": max(amts) if amts else 0.0,
            "periodic": periodic,
            "median_gap": med_gap,
            "regularity": regularity,
            "last_date": dates_ok[-1] if dates_ok else None,
            "channel": str(txs[-1].get("channel") or ""),
        }
    return out


def _channel_mix(history: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    grand = 0.0
    for tx in history:
        ch = str(tx.get("channel") or "")
        amt = float(tx.get("amount_gbp") or 0.0)
        totals[ch] += amt
        grand += amt
    if grand <= 0:
        return {}
    return {ch: totals[ch] / grand for ch in totals}


def _alerted_txs(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {t["transaction_id"]: t for t in bundle.get("customer_transactions") or []}
    for t in bundle.get("transactions") or []:
        by_id[t["transaction_id"]] = t
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for alert in bundle.get("alerts") or []:
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


def build_case_features(bundle: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic feature bag for one case bundle."""
    case = bundle.get("case") or {}
    window = case.get("review_window") or {}
    window_from = window.get("from")
    customer = bundle.get("customer") or {}
    declared = customer.get("declared") or {}
    expected_channels = {
        norm_name(c) for c in (declared.get("expected_channels") or []) if c
    }

    history = _history_before(bundle.get("customer_transactions") or [], window_from)
    norms = _amount_norms(history)
    monthly_totals = _monthly_inbound_totals(history)
    hist_month_median = _median(monthly_totals) if monthly_totals else 0.0
    cp_hist = _cp_history(history)
    hist_channel_mix = _channel_mix(history)

    alerted = _alerted_txs(bundle)
    alert_amount_scores: list[dict[str, Any]] = []
    periodic_legs: list[str] = []
    profile_like_legs: list[str] = []
    novel_large: list[dict[str, Any]] = []
    undeclared_value = 0.0
    alert_total = 0.0
    cash_hist_amts = [
        float(t.get("amount_gbp") or 0.0)
        for t in history
        if str(t.get("channel") or "") in _CASH_CHANNELS
    ]
    cash_hist_median = _median(cash_hist_amts) if cash_hist_amts else 0.0

    # Alert-window inbound total (calendar months touched by alerted ins).
    alert_month_inbound = 0.0
    alert_months: set[str] = set()
    for tx in alerted:
        if tx.get("direction") == "in":
            dt = _tx_date(tx)
            if dt:
                alert_months.add(f"{dt.year:04d}-{dt.month:02d}")
    if alert_months:
        for tx in bundle.get("customer_transactions") or []:
            if tx.get("direction") != "in":
                continue
            dt = _tx_date(tx)
            if not dt:
                continue
            key = f"{dt.year:04d}-{dt.month:02d}"
            if key in alert_months:
                alert_month_inbound += float(tx.get("amount_gbp") or 0.0)

    for tx in alerted:
        tid = tx["transaction_id"]
        amt = float(tx.get("amount_gbp") or 0.0)
        alert_total += amt
        key = _bucket_key(tx)
        norm = norms.get(key)
        amount_z = 0.0
        within_typical = False
        if norm:
            amount_z = (amt - norm["median"]) / norm["iqr"]
            within_typical = amt <= norm["high"]
        cp_raw = tx.get("counterparty_name")
        cp = norm_name(cp_raw)
        prior = cp_hist.get(cp) if cp else None
        is_periodic = False
        if prior and prior.get("periodic"):
            med = float(prior["median"] or 0.0)
            if med > 0 and abs(amt - med) / med <= 0.10:
                # Gap vs series median.
                last = prior.get("last_date")
                dt = _tx_date(tx)
                med_gap = float(prior.get("median_gap") or 0.0)
                if last and dt and med_gap > 0:
                    gap = abs((dt.date() - last.date()).days)
                    if abs(gap - med_gap) <= max(3.0, 0.25 * med_gap):
                        is_periodic = True
                else:
                    is_periodic = True
        # Only a confirmed periodic series counts as profile-like leftover.
        # (Do not treat every typical BACS/DD to a known CP as salary-shaped.)
        if is_periodic:
            periodic_legs.append(tid)
            profile_like_legs.append(tid)

        novel = False
        channel_high = float((norm or {}).get("high") or 0.0)
        if cp:
            prior_count = int(prior["count"]) if prior else 0
            cp_med = float(prior["median"]) if prior else 0.0
            threshold = max(2.0 * cp_med if cp_med else 0.0, channel_high, _NOVEL_FLOOR_GBP)
            if prior_count <= 1 and amt >= threshold:
                novel = True
                novel_large.append(
                    {
                        "transaction_id": tid,
                        "counterparty_name": cp_raw,
                        "amount_gbp": amt,
                        "prior_count": prior_count,
                    }
                )
        elif amt >= max(channel_high, _NOVEL_FLOOR_GBP):
            # Unnamed (e.g. cash) large vs channel history.
            if not norm or amt >= norm["high"]:
                novel = True
                novel_large.append(
                    {
                        "transaction_id": tid,
                        "counterparty_name": None,
                        "amount_gbp": amt,
                        "prior_count": 0,
                    }
                )

        ch = str(tx.get("channel") or "")
        ch_norm = norm_name(ch.replace("_", " "))
        # expected_channels often use human labels like "Cash deposits".
        declared_ok = True
        if expected_channels:
            declared_ok = any(
                ch_norm in exp or exp in ch_norm or ch.replace("_", " ") in exp
                for exp in expected_channels
            )
            # Also accept raw channel token overlap.
            if not declared_ok:
                for exp in expected_channels:
                    if any(tok and tok in exp for tok in ch.split("_")):
                        declared_ok = True
                        break
        if not declared_ok:
            undeclared_value += amt

        alert_amount_scores.append(
            {
                "transaction_id": tid,
                "amount_gbp": amt,
                "channel": ch,
                "direction": tx.get("direction"),
                "amount_z": round(amount_z, 3),
                "within_typical": within_typical,
                "periodic": is_periodic,
                "novel_large": novel,
                "counterparty_name": cp_raw,
            }
        )

    undeclared_share = (undeclared_value / alert_total) if alert_total > 0 else 0.0
    inbound_vs_hist = (
        (alert_month_inbound / hist_month_median) if hist_month_median > 0 else 0.0
    )

    # Cash novelty: alerted cash vs historical cash median.
    alert_cash = [
        t
        for t in alerted
        if str(t.get("channel") or "") in _CASH_CHANNELS
    ]
    cash_vs_hist = 0.0
    if alert_cash and cash_hist_median > 0:
        max_cash = max(float(t.get("amount_gbp") or 0.0) for t in alert_cash)
        cash_vs_hist = max_cash / cash_hist_median
    elif alert_cash and cash_hist_median <= 0:
        cash_vs_hist = 99.0  # first cash activity

    # Historical cash share for channel-mix novelty.
    hist_cash_share = sum(
        hist_channel_mix.get(ch, 0.0) for ch in _CASH_CHANNELS
    )
    alert_cash_share = 0.0
    if alert_total > 0:
        alert_cash_share = (
            sum(float(t.get("amount_gbp") or 0.0) for t in alert_cash) / alert_total
        )

    novel_large.sort(key=lambda r: (-float(r["amount_gbp"]), r["transaction_id"]))
    periodic_legs = sorted(set(periodic_legs))
    profile_like_legs = sorted(set(profile_like_legs))

    return {
        "alert_amount_scores": alert_amount_scores,
        "periodic_legs": periodic_legs,
        "profile_like_legs": profile_like_legs,
        "novel_large_cps": novel_large,
        "novel_large_tx_ids": [r["transaction_id"] for r in novel_large],
        "undeclared_channel_share": round(undeclared_share, 4),
        "inbound_month_vs_history": round(inbound_vs_hist, 4),
        "hist_month_median_gbp": round(hist_month_median, 2),
        "alert_month_inbound_gbp": round(alert_month_inbound, 2),
        "cash_vs_history": round(cash_vs_hist, 4),
        "hist_cash_share": round(hist_cash_share, 4),
        "alert_cash_share": round(alert_cash_share, 4),
        "history_tx_count": len(history),
    }


def attach_stats(bundle: dict[str, Any]) -> dict[str, Any]:
    """Compute and store features on the bundle; return the feature dict."""
    features = build_case_features(bundle)
    bundle["stats"] = features
    return features


def corroboration_sentence(
    features: dict[str, Any] | None, evidence_ids: list[str]
) -> str | None:
    """One factual sentence when a cited CLOSE leg looks periodic/typical."""
    if not features:
        return None
    cited = set(evidence_ids or [])
    scores = features.get("alert_amount_scores") or []
    periodic = set(features.get("periodic_legs") or [])
    for row in scores:
        tid = row.get("transaction_id")
        if tid not in cited:
            continue
        if tid in periodic or row.get("periodic"):
            cp = row.get("counterparty_name") or "the same counterparty"
            ch = row.get("channel") or "transfer"
            amt = float(row.get("amount_gbp") or 0.0)
            return (
                f"This aligns with the customer's usual {ch} pattern with {cp}"
                + (f" around £{amt:,.2f}." if amt else ".")
            )
        if row.get("within_typical") and not row.get("novel_large"):
            ch = row.get("channel") or "activity"
            return (
                f"The alerted {ch} amount is within this customer's historical "
                f"range for that channel."
            )
    return None
