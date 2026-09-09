# Retail Opportunity & Purchase Assistant

Monitors products across e-commerce sites, detects buying opportunities, scores
their economic interest, sends Discord alerts, and — only for explicitly
pre-authorized products and merchants — can prepare or execute a purchase.

Built incrementally, one phase at a time. See "Status" below for what exists
right now.

## Status: Phase 0 — Project bootstrap

- No monitoring, no connectors, no Discord, no purchasing logic exists yet.
- This phase only proves the project structure, test runner, and linter work.

## Safety principles (apply from day one)

- **Deny by default.** Nothing is authorized unless an explicit rule says so.
- **No autobuy for unknown products.** Automated purchase requires exact
  product identification (EAN/GTIN/SKU match) and a human-configured rule.
- **No anti-bot circumvention.** The system never bypasses CAPTCHAs, queues,
  per-customer limits, or anti-fraud/payment protections.
- **Secrets never touch Git.** Tokens, API keys, and credentials live in
  environment variables (`.env`, gitignored) — never in code, logs, or the
  database. See `.env.example` for the variable names.
- **Dry-run first.** Purchase logic runs in simulation mode until explicitly
  proven safe and enabled.

## Project layout

```
app/            application entrypoint
config/         centralized configuration (env-driven, no secrets in code)
products/       product/merchant/listing domain models
connectors/     one module per merchant, exposing a common interface
engine/         matching, decision, risk, and profitability logic
notifications/  outbound notifications (discord/ subpackage)
database/       persistence layer
checkout/       purchase state machine and execution (dry-run first)
security/       budget limits, kill switch, authorization checks
tests/          test suite
scripts/        one-off operational scripts
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running

```bash
python -m app.main
```

## Tests

```bash
pytest
```

## Lint & format

```bash
ruff check .
ruff format .
```
