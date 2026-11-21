# Alert-closing system

Deterministic Python 3 (standard library only) decision engine for the four institution folders.

## How it decides

1. Load the institution folder (`cases`, `alerts`, `customers`, `transactions`, `evidence`, `previous_reviews`, `internal_list`).
2. Parse structured fields from each evidence Markdown file (amounts, parties, accounts, dates).
3. Build per-customer statistical features from pre-review-window history (amount norms by channel, periodicity, counterparty novelty, channel vs KYC). Stats are priors/demoters only — they never CLOSE alone.
4. For every alert in a case, try positive explanations first (invoice/contract match, own-account statement, group structure, cash/takings reconciliation, documented events/gifts/refunds, completion-statement investment funding, group booking collections, early-redemption need letters, returning customer, list-match exoneration).
5. Apply hard escalate gates (sending-bank fraud reports, mule language in chats, unexplained rapid pass-through, structuring, confirmed listed party, unexplained early redemption, high-risk corridor without docs).
6. For payments/FX, decide the **held payment** first (invoice gate / own-name / match), then fold in the other alerts.
7. If unclear but not suspicious → **RFI** with a concrete ask naming the payment where possible (defaults may cite novelty / monthly surge).
8. Only **CLOSE** when every alert is explained **and** residual unexplained value is immaterial or ≤ the declared TM01 baseline (soft identity/profile codes do not count as value coverage; uncited novel-large legs demote to RFI).
9. CLOSE gates reject thin stories in general terms: named counterparties for refund CLOSES, invoice/event party alignment, majority-leg coverage for multi-tx rules, and no phrase-only `WITHIN_PROFILE` / prior-review `RETURNING_CUSTOMER` closes.
10. Optional: if `OPENROUTER_API_KEY` is set, polish the reason text at temperature 0. Verdict, reason code, and evidence IDs stay locked. No case IDs, customer names, or development amounts are hardcoded.

Institution-specific thresholds from each `POLICY.md` are encoded in `policy.py`.

## Model

Default: no language model (fully deterministic rules + templates).

Optional reason polish via OpenRouter chat completions when `OPENROUTER_API_KEY` is present. Default model is `baidu/ernie-5.5-flash-preview:free` (`OPENROUTER_MODEL` overrides). Polishing cannot change the decision.

## Run

```bash
system/run institutions/bank output/bank.jsonl
system/run institutions/emoney output/emoney.jsonl
system/run institutions/investment output/investment.jsonl
system/run institutions/payments_fx output/payments_fx.jsonl
```

Regression on clear-cut cases:

```bash
python3 tests/run_audit.py
python3 tests/stats_smoke.py
```

## OpenRouter smoke (TypeScript)

Small `@openrouter/sdk` package that makes one end-to-end request and prints the response text. Intended as a scaffold for later LLM-written verdict reasons.

- **Model slug:** `baidu/ernie-5.5-flash-preview:free` ($0 prompt/completion; see https://openrouter.ai/baidu/ernie-5.5-flash-preview:free)
- **Temperature:** `0`
- **API key:** read from `OPENROUTER_API_KEY` (never committed). Create one at https://openrouter.ai/keys

```bash
cd system/openrouter
npm install
export OPENROUTER_API_KEY=sk-or-...   # required; exits non-zero if unset
npm run smoke
```

If the key is missing, the script prints how to create one and exits with status 1.

## Runtime and cost

On this development set (1,000 cases per institution), a full run is a few seconds per institution with no API key ($0). A 200-case run stays well under the 30-minute / ~$4 budgets even with optional reason polish (default free OpenRouter model). The TypeScript OpenRouter smoke is optional and uses the free model above when a key is set.
