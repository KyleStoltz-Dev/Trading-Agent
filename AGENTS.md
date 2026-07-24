# Trading Agent development guide

## Product boundary

This application is a decision-support and journaling tool. It must not autonomously
place, modify, or cancel orders. Broker integrations are read-only until a separately
reviewed order-preview flow exists, and every submission requires explicit human approval.

`app/trading-rules.json` is the runtime policy for the product. Do not add or expose a tool
without explicit policy metadata and a pre-execution policy-hook check. Never bypass a policy
failure, confirmation hook, or policy-hash mismatch.

## Domain rules

- Separate chart observations from interpretations and proposed scenarios.
- Never manufacture prices, timestamps, news, fills, or indicator values.
- Treat Wyckoff and smart-money labels as testable hypotheses, not facts.
- Risk and position sizing are deterministic code paths, never model arithmetic.
- Preserve source, market timestamp, instrument, venue, and timeframe with evidence.
- Do not encode a setup as an edge until a reviewed sample supports it.
- Never commit credentials, account data, private journal exports, or raw broker payloads.
- Keep model providers behind the provider protocol. OpenAI and Anthropic SDKs are optional
  adapters; domain services must not depend directly on either.

## Verification

Run before committing:

```bash
ruff check .
pytest
```
