# Tests

## Audit regression suite

`tests/audit/` holds a hand-logic set of **clear-cut** development cases used to catch regressions.

| Institution   | Cases | CLOSE | RFI | ESCALATE |
|---------------|------:|------:|----:|---------:|
| bank          | ~80   | yes   | yes | yes      |
| emoney        | ~90   | yes   | yes | yes      |
| investment    | ~70   | yes   | yes | yes      |
| payments_fx   | ~65   | yes   | yes | yes      |

Patterns covered include fraud reports, unexplained TM02+TM04 networks, TM10 early redemption, exact invoice/cash reconciliations, clean gift letters, TM22-only list outcomes, own-account 5× RFIs, and the payments_fx £10k invoice gate.

Ambiguous multi-conflict cases are excluded on purpose.

### Run

From the repo root:

```bash
python3 tests/run_audit.py
python3 tests/run_audit.py --institution bank
```

Exit code `0` means every audit case matched on verdict, an accepted reason code, and required evidence citations.
