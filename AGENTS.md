# Trading Agent development guide

## Product boundary

This application is a decision-support and journaling tool. It must not autonomously
place, modify, or cancel orders. Broker integrations are read-only until a separately
reviewed order-preview flow exists, and every submission requires explicit human approval.

## Domain rules

- Separate chart observations from interpretations and proposed scenarios.
- Never manufacture prices, timestamps, news, fills, or indicator values.
- Treat Wyckoff and smart-money labels as testable hypotheses, not facts.
- Risk and position sizing are deterministic code paths, never model arithmetic.
- Preserve source, market timestamp, instrument, venue, and timeframe with evidence.
- Do not encode a setup as an edge until a reviewed sample supports it.
- Never commit credentials, account data, private journal exports, or raw broker payloads.

## Verification

Run before committing:

```bash
ruff check .
pytest
```

