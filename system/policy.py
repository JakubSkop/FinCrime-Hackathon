"""Per-institution policy settings taken from each POLICY.md."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


HR_COUNTRIES = {
    "Afghanistan",
    "Belarus",
    "Democratic Republic of the Congo",
    "Haiti",
    "Iran",
    "Iraq",
    "Lebanon",
    "Libya",
    "Mali",
    "Myanmar",
    "North Korea",
    "Russia",
    "Somalia",
    "South Sudan",
    "Syria",
    "Venezuela",
    "Yemen",
}

HR_COUNTRY_CODES = {
    "AF",
    "BY",
    "CD",
    "HT",
    "IR",
    "IQ",
    "LB",
    "LY",
    "ML",
    "MM",
    "KP",
    "RU",
    "SO",
    "SS",
    "SY",
    "VE",
    "YE",
}


CLOSE_CODES = {
    "COLLECTION_DOCUMENTED",
    "BUSINESS_RECORDS_RECONCILE",
    "DOCUMENTED_EVENT",
    "INVOICE_MATCHES",
    "OWN_ACCOUNT",
    "GROUP_COMPANY",
    "THIRD_PARTY_DOCUMENTED",
    "REFUND_OF_EARLIER_PAYMENT",
    "RETURNING_CUSTOMER",
    "LIST_MATCH_DIFFERENT_PARTY",
    "WITHIN_PROFILE",
}

RFI_CODES = {
    "RFI_PURPOSE_DOCUMENTS",
    "RFI_BUSINESS_RECORDS",
    "RFI_SOURCE_OF_FUNDS",
    "RFI_COUNTERPARTY_DETAILS",
    "RFI_UPDATED_PROFILE",
}

ESCALATE_CODES = {
    "ESC_MULE_ACTIVITY",
    "ESC_STRUCTURING",
    "ESC_UNEXPLAINED_FUNDS",
    "ESC_PASS_THROUGH",
    "ESC_INVOICE_MISMATCH",
    "ESC_HIGH_RISK_CORRIDOR",
    "ESC_ACCOUNT_MISUSE",
    "ESC_THIRD_PARTY_FUNDING",
    "ESC_EARLY_REDEMPTION",
    "ESC_LISTED_PARTY",
}

REASON_BY_VERDICT = {
    "CLOSE": CLOSE_CODES,
    "RFI": RFI_CODES,
    "ESCALATE": ESCALATE_CODES,
}


@dataclass(frozen=True)
class InstitutionPolicy:
    name: str
    rules: frozenset[str]
    # TM01
    tm01_individual_multiple: float
    tm01_business_multiple: float
    tm01_uses_annual_payment_volume: bool
    # TM03 structuring band
    tm03_min: float | None
    tm03_max: float | None
    tm03_count: int | None
    # TM05 cash
    tm05_individual: float | None
    tm05_business: float | None
    # TM04
    tm04_limit: int | None
    # payments_fx invoice gate
    invoice_gate_gbp: float | None
    own_account_rfi_multiple: float = 5.0


POLICIES: dict[str, InstitutionPolicy] = {
    "bank": InstitutionPolicy(
        name="bank",
        rules=frozenset(
            {"TM01", "TM02", "TM03", "TM04", "TM05", "TM06", "TM07", "TM22"}
        ),
        tm01_individual_multiple=3.0,
        tm01_business_multiple=2.0,
        tm01_uses_annual_payment_volume=False,
        tm03_min=9000.0,
        tm03_max=9999.99,
        tm03_count=3,
        tm05_individual=10000.0,
        tm05_business=25000.0,
        tm04_limit=6,
        invoice_gate_gbp=None,
    ),
    "emoney": InstitutionPolicy(
        name="emoney",
        rules=frozenset(
            {"TM01", "TM02", "TM03", "TM04", "TM05", "TM06", "TM07", "TM22"}
        ),
        tm01_individual_multiple=3.0,
        tm01_business_multiple=2.0,
        tm01_uses_annual_payment_volume=False,
        tm03_min=800.0,
        tm03_max=999.99,
        tm03_count=4,
        tm05_individual=1000.0,
        tm05_business=5000.0,
        tm04_limit=6,
        invoice_gate_gbp=None,
    ),
    "investment": InstitutionPolicy(
        name="investment",
        rules=frozenset({"TM01", "TM02", "TM06", "TM07", "TM08", "TM10", "TM22"}),
        tm01_individual_multiple=3.0,
        tm01_business_multiple=2.0,
        tm01_uses_annual_payment_volume=False,
        tm03_min=None,
        tm03_max=None,
        tm03_count=None,
        tm05_individual=None,
        tm05_business=None,
        tm04_limit=None,
        invoice_gate_gbp=None,
    ),
    "payments_fx": InstitutionPolicy(
        name="payments_fx",
        rules=frozenset(
            {"TM01", "TM02", "TM03", "TM04", "TM06", "TM07", "TM08", "TM22"}
        ),
        tm01_individual_multiple=2.0,
        tm01_business_multiple=2.0,
        tm01_uses_annual_payment_volume=True,
        tm03_min=9000.0,
        tm03_max=9999.99,
        tm03_count=3,
        tm05_individual=None,
        tm05_business=None,
        tm04_limit=25,
        invoice_gate_gbp=10000.0,
    ),
}


def get_policy(institution: str) -> InstitutionPolicy:
    if institution not in POLICIES:
        raise ValueError(f"Unknown institution: {institution}")
    return POLICIES[institution]


def declared_baseline_gbp(customer: dict[str, Any], policy: InstitutionPolicy) -> float:
    declared = customer.get("declared") or {}
    if policy.tm01_uses_annual_payment_volume:
        annual = declared.get("annual_payment_volume_gbp")
        if annual is None:
            annual = declared.get("expected_annual_payment_volume_gbp")
        if annual is None:
            annual = declared.get("annual_turnover_gbp")
        if annual:
            return float(annual) / 12.0
        return 0.0
    if customer.get("kind") == "business":
        turnover = declared.get("annual_turnover_gbp")
        if turnover:
            return float(turnover) / 12.0
        monthly = declared.get("monthly_turnover_gbp")
        return float(monthly or 0.0)
    return float(declared.get("monthly_income_gbp") or 0.0)


def validate_reason_code(verdict: str, reason_code: str) -> bool:
    return reason_code in REASON_BY_VERDICT.get(verdict, set())
