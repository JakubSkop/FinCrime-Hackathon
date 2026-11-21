# OpenRouter smoke client

TypeScript package using the official [`@openrouter/sdk`](https://www.npmjs.com/package/@openrouter/sdk) to prove one working OpenRouter request. Later this can draft LLM-written verdict reason text; the hackathon decision engine remains rules-first.

## Model

| Setting | Value |
|---|---|
| Slug | `openai/gpt-4o-mini` |
| Temperature | `0` |
| Catalog | https://openrouter.ai/models |

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
