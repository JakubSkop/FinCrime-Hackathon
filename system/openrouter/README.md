# OpenRouter smoke client

TypeScript package using the official [`@openrouter/sdk`](https://www.npmjs.com/package/@openrouter/sdk) to prove one working OpenRouter request. Later this can draft LLM-written verdict reason text; the hackathon decision engine remains rules-first.

## Model

| Setting | Value |
|---|---|
| Slug | `baidu/ernie-5.5-flash-preview:free` |
| Pricing | `$0` prompt / `$0` completion (free endpoint; rate-limited) |
| Temperature | `0` |
| Catalog | https://openrouter.ai/baidu/ernie-5.5-flash-preview:free |

Chosen as a currently available free chat model (`:free` suffix) so smoke works with a $0 OpenRouter credit limit. Confirmed on the OpenRouter model page and models API as $0/$0; subject to free-tier rate limits.

## Setup

1. Create an API key at https://openrouter.ai/keys
2. Export it (do not commit `.env`):

```bash
export OPENROUTER_API_KEY=sk-or-...
```

Optional: copy `.env.example` to `.env` and fill the key for local use (`.env` is gitignored).

## Run

```bash
cd system/openrouter
npm install
npm run smoke
```

On success, the process prints the model response text (expected phrase: `OpenRouter smoke OK`) and exits 0.

If `OPENROUTER_API_KEY` is unset or empty, it prints a clear message pointing at https://openrouter.ai/keys and exits non-zero.

## Typecheck

```bash
npm run typecheck
```
