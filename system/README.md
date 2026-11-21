# Alert-closing system

Deterministic Python 3 (standard library only) decision engine for the four institution folders.

## How it decides

1. Load the institution folder (`cases`, `alerts`, `customers`, `transactions`, `evidence`, `previous_reviews`, `internal_list`).
2. Parse structured fields from each evidence Markdown file (amounts, parties, accounts, dates).
3. For every alert in a case, try positive explanations first (invoice/contract match, own-account statement, group structure, cash/takings reconciliation, documented events/gifts/refunds, returning customer, list-match exoneration).
4. Apply hard escalate gates (sending-bank fraud reports, mule language in chats, unexplained rapid pass-through, structuring without records, confirmed listed party, early redemption, high-risk corridor without docs).
5. If there is no suspicion but a single document would settle the case, issue an RFI with an explicit ask.
6. Aggregate alert dispositions conservatively: **CLOSE only when every alert is positively explained**; any escalate wins; otherwise RFI; never close on a partial explanation.

Institution-specific thresholds from each `POLICY.md` are encoded in `policy.py` (TM01 multiples, cash/structuring bands, TM04 limits, payments_fx £10k invoice gate, investment TM08/TM10).

## Model

No language model. Decisions and reasons are produced by fixed rules and templates, so runs are reproducible.

## Run

From the repo root (paths may be absolute; the process may be started inside `system/`):

```bash
system/run institutions/bank output/bank.jsonl
system/run institutions/emoney output/emoney.jsonl
system/run institutions/investment output/investment.jsonl
system/run institutions/payments_fx output/payments_fx.jsonl
```

## Runtime and cost

On this development set (1,000 cases per institution):

| Institution   | Wall time |
|---------------|-----------|
| bank          | ~5s       |
| emoney        | ~4s       |
| investment    | ~1s       |
| payments_fx   | ~3s       |

A 200-case run is typically under 2 seconds and costs $0 (no API calls).
