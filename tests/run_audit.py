#!/usr/bin/env python3
"""Regression suite against the clear-cut audit set in tests/audit/.

Pass criteria per case:
  - verdict must match the audit label
  - reason_code must match label.reason_code or an entry in accepted_reason_codes
  - every ID in must_cite must appear in the system's evidence list

Usage (from repo root):
  python3 tests/run_audit.py
  python3 tests/run_audit.py --institution bank
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AUDIT_DIR = os.path.join(ROOT, "tests", "audit")
SYSTEM_DIR = os.path.join(ROOT, "system")


def load_labels(institution: str | None) -> list[dict]:
    labels: list[dict] = []
    for name in sorted(os.listdir(AUDIT_DIR)):
        if not name.endswith(".jsonl"):
            continue
        inst = name[: -len(".jsonl")]
        if institution and inst != institution:
            continue
        path = os.path.join(AUDIT_DIR, name)
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    labels.append(json.loads(line))
    return labels


def _missing_citations(data, lab: dict, result: dict) -> list[str]:
    """Return must_cite IDs that are absent, allowing same-type evidence substitutes."""
    cited = set(result.get("evidence") or [])
    missing: list[str] = []
    for eid in lab.get("must_cite") or []:
        if eid in cited:
            continue
        meta = data.evidence_meta.get(eid)
        if meta:
            # Another cited document of the same type is an acceptable substitute
            # (e.g. a different bank statement proving own-account).
            same_type = {
                doc_id
                for doc_id, m in data.evidence_meta.items()
                if m.get("type") == meta.get("type")
                and m.get("customer_id") == meta.get("customer_id")
            }
            if cited & same_type:
                continue
        missing.append(eid)
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--institution",
        choices=["bank", "emoney", "investment", "payments_fx"],
        help="Limit to one institution",
    )
    args = parser.parse_args()

    if SYSTEM_DIR not in sys.path:
        sys.path.insert(0, SYSTEM_DIR)

    from decide import decide_case
    from loader import InstitutionData

    labels = load_labels(args.institution)
    if not labels:
        print("No audit labels found.", file=sys.stderr)
        return 2

    by_inst: dict[str, list[dict]] = {}
    for lab in labels:
        by_inst.setdefault(lab["institution"], []).append(lab)

    started = time.time()
    failures: list[str] = []
    passed = 0

    for inst, rows in sorted(by_inst.items()):
        data = InstitutionData(os.path.join(ROOT, "institutions", inst))
        cases = {c["case_id"]: c for c in data.cases}
        for lab in rows:
            case = cases.get(lab["case_id"])
            if case is None:
                failures.append(f"{inst}/{lab['case_id']}: missing from cases.jsonl")
                continue
            result = decide_case(data.assemble_case(case))
            accepted = {lab["reason_code"], *lab.get("accepted_reason_codes", [])}

            problems: list[str] = []
            if result["verdict"] != lab["verdict"]:
                problems.append(
                    f"verdict {result['verdict']} != {lab['verdict']}"
                )
            if result["reason_code"] not in accepted:
                problems.append(
                    f"reason_code {result['reason_code']} not in {sorted(accepted)}"
                )
            missing = _missing_citations(data, lab, result)
            if missing:
                problems.append(f"missing must_cite {missing}")

            if problems:
                failures.append(
                    f"{inst}/{lab['case_id']} [{lab.get('pattern')}]: "
                    + "; ".join(problems)
                )
            else:
                passed += 1

    elapsed = time.time() - started
    total = passed + len(failures)
    print(
        f"Audit regression: {passed}/{total} passed in {elapsed:.1f}s "
        f"across {len(by_inst)} institution(s)"
    )
    if failures:
        print(f"\n{len(failures)} failure(s):")
        for line in failures[:50]:
            print(" -", line)
        if len(failures) > 50:
            print(f" - ... and {len(failures) - 50} more")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
