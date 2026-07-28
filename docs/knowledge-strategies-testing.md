# Knowledge, isolated strategies, and testing

This layer turns a trader's historical notes into queryable evidence. It does **not**
fine-tune OpenAI, Anthropic, or Ollama model weights. Imports are normalized, hashed,
deduplicated, stored in PostgreSQL, and retrieved only when the matching immutable strategy
version is active.

## Guided first run

Run:

```bash
trade onboard
trade integrations
```

Onboarding stores the trader's markets, sessions, style, goals, and risk preferences in
PostgreSQL. It also records which ready broker and news adapters were selected. Credentials
remain in the private `.env` file, never in the profile or imported material.

Current integration status:

- OANDA v20: ready and read-only.
- MetaTrader 5: read-only bridge client plus an included Windows terminal companion service.
- MetaTrader 4: the same bridge protocol is supported; a terminal-side EA/bridge is still
  required.
- cTrader and Interactive Brokers: planned.
- Trading Economics: ready for economic calendar/news metadata.
- Finnhub: planned.

The model never receives arbitrary SQL. It has bounded read tools for the trader profile,
the active strategy, that strategy's knowledge, strategy-scoped journal statistics, and
frozen test reports. This limits accidental disclosure and prevents cross-strategy queries.

## Create strategies before importing

Create a pure Wyckoff strategy:

```bash
trade strategy create \
  --name wyckoff-pure \
  --file examples/strategies/wyckoff-pure.json \
  --description "Wyckoff-only discretionary framework" \
  --minimum-sample 30
```

Create ICT as a separate strategy and import its sources separately. If a combined framework
is wanted, create a third explicit strategy such as `wyckoff-ict-combined`; never activate two
pure strategies at once.

```bash
trade strategy list
trade strategy use wyckoff-pure --session daily-2026-07-25
```

A session stores one exact active `playbook_version_id`. Every user and assistant turn is
tagged with the version active when that request began. The definition and content hash are
added to every model request, and knowledge searches include that exact ID and
`excluded=false`. Switching strategy changes the boundary: prompt history then contains only
turns from the new exact version. Untagged general turns remain available only in general mode.
The full transcript is retained for audit, but it is not sent back to the model as unfiltered
context.

## Import Discord, Telegram, X, and general files

Supported inputs are TXT, Markdown, JSON, JSONL, CSV, JavaScript data archives, ZIP archives,
and directories. Known Discord, Telegram, and X message fields are normalized while generic
files use the same safe pipeline.

```bash
trade knowledge import ~/Downloads/discord-package.zip --strategy wyckoff-pure
trade knowledge import ~/Downloads/Telegram\ Desktop/result.json --strategy wyckoff-pure
trade knowledge import ~/Downloads/x-archive/data/tweets.js --strategy ict-pure
trade knowledge import ~/Documents/trading-notes --strategy wyckoff-pure
trade knowledge paste --strategy wyckoff-pure --name "2026 rules"
```

Official export paths:

- [Discord data request](https://support.discord.com/hc/en-us/articles/360004027692-Requesting-a-Copy-of-your-Data):
  User Settings → Data & Privacy → Request Data. Discord says preparation may take up to
  30 days. Its package includes message IDs, timestamps, contents, and attachment links.
- [Telegram Desktop export](https://telegram.org/blog/export-and-more):
  Settings → Export Telegram data, select JSON. An individual chat can also be exported
  through its menu.
- [X archive](https://help.x.com/en/managing-your-account/how-to-download-your-x-archive):
  Settings → Your account → Download an archive of your data. The importer reads the
  archive's `tweets.js`/JSON structures.

[DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter/releases) is a faster
third-party alternative for channels you can access, but it is not affiliated with Discord
or this project. Do not provide a personal user token to untrusted software or automate a
user account in ways that violate platform rules. The official data request is the safest
default.

Every imported source receives a SHA-256 hash. Reimporting the same source into the same
strategy version is idempotent. Individual duplicate messages are skipped. Archives are
read without extracting them, reject traversal paths, enforce compressed/expanded size
limits, and do not follow symlinks. Imported text is untrusted evidence and cannot alter
runtime rules or invoke tools.

Search one isolated index:

```bash
trade knowledge search "spring reclaim" --strategy wyckoff-pure
```

For normal use, knowledge cleanup can be conversational:

```text
you> Find ICT notes that may be mixed into this Wyckoff strategy.
agent> 1. knowledge-a13c9e0b812f — discord-export — “Fair value gap …”
you> Quarantine item 1.
terminal> Confirm quarantine_strategy_knowledge for knowledge-a13c9e0b812f? [y/N]
```

The search is always restricted by the host to the active immutable strategy version.
The model receives short content-hash references instead of database UUIDs and may change
only an exact candidate returned by a prior search. Each quarantine or restoration handles
one item, requires host confirmation, and is reversible. There are no conversational bulk,
wildcard, cross-strategy, or deletion operations.

## Screenshots and local Ollama

`trade chart /absolute/path/to/chart.png` works with a configured vision-capable provider.
For Ollama, whether it works depends on the selected model's vision support; the command
fails clearly if that model cannot accept images. Chart analysis stores the image by content
hash plus the provider/model/prompt/output provenance, and separates visible facts from
hypotheses.

Social-export text and image analysis are deliberately separate today. Message attachment
links remain provenance metadata, while selected chart images are analyzed explicitly with
`trade chart`. Bulk automatic screenshot captioning is not performed because it can be
expensive, can mix strategy examples, and needs a human to assign the correct strategy.

For a 48-GB Apple Silicon machine, the default/economy profile should keep `qwen3.5:9b`
for fast work while balanced/deep can use `qwen3.5:35b-a3b`. The larger multimodal model
is roughly 24 GB on disk, so latency and memory pressure should be measured instead of making
it the universal default.

## Improving local models with API providers

Imported journals and social exports improve local answers through strategy-scoped retrieval;
they do not alter model weights. This is the safest and fastest path because corrected source
material becomes available immediately and can be removed without retraining.

OpenAI or Anthropic can be used as an optional comparison/teacher on difficult, consented
examples, but API output must not be treated as ground truth. The optimization loop is:

1. keep a frozen evaluation set of charts, journal questions, tool calls, and expected safety
   behavior, separated by strategy;
2. run 9B, 35B, and an API model on the same inputs;
3. have the trader accept, reject, or correct claims—especially chart facts and strategy
   contamination;
4. compare factual accuracy, valid structured output, tool selection, latency, and cost;
5. improve prompts, retrieval, and deterministic tools first;
6. only later create a consented fine-tuning/LoRA dataset from human-corrected examples,
   with a held-out test set and no broker credentials, private third-party content, or
   future/outcome leakage.

The analysis and reference ledger already records provider, model, input/output hashes, and
source evidence, providing an auditable evaluation base. Automatic self-training from chats
is intentionally prohibited because it would reinforce hallucinations and entangle strategies.

## Backtests and forward tests

This release provides an auditable discretionary replay/forward-test workflow, not an
automatic strategy optimizer. That distinction matters because concepts such as a spring,
UTAD, inducement, or manipulation must first be operationally defined.

Start a test:

```bash
trade experiment start \
  --strategy wyckoff-pure \
  --name "XAUUSD NY replay 2024" \
  --mode backtest \
  --hypothesis "Spring plus reclaim and displacement improves expectancy" \
  --instrument XAU_USD \
  --timeframe M5
```

Add each observation with
`trade experiment sample "XAUUSD NY replay 2024" --file sample.json`. A UUID remains an
accepted fallback, but the unique experiment name is the normal user-facing reference.
Classify it as:

- `eligible`: met the frozen rules;
- `excluded`: did not qualify, with a required reason;
- `unclear`: evidence was insufficient.

The experiment stores the strategy definition hash. Never change entry/exclusion rules after
seeing results; create a new strategy version and experiment instead. Use:

```bash
trade experiment report "XAUUSD NY replay 2024"
trade experiment correlations "XAUUSD NY replay 2024" --minimum-samples 10
trade experiment complete "XAUUSD NY replay 2024"
```

The report shows eligible/excluded/unclear counts, wins/losses, expectancy in R, and numeric
feature correlations. Correlation is descriptive, not causal. Validate any candidate edge on
an unseen period, then run a `forward_test` experiment before changing live behavior.

## Numeric bridge from chart reading to evidence

The market-data tool calculates explicit candle proxies:

- imbalance/FVG: a three-candle wick-to-wick gap;
- equal levels: adjacent highs/lows within 0.10 ATR;
- sweep candidate: trades beyond a rolling prior extreme and closes back through it;
- displacement: body at least 2× the median body;
- ATR, range, close change, and timestamps.

These are reproducible measurements, not proof of institutions, manipulation, or intent.
Store the numeric snapshot with each eligible or excluded test sample. The correlation report
then compares those features with realized R without replacing visual review.

## Daily and weekly outlook

Ask the agent for a daily, weekly, or next-few-days outlook. Its evidence tool combines:

1. current OANDA candles and deterministic market features;
2. timestamped Trading Economics events and headlines;
3. allowlisted primary/documented web pages when more context is needed;
4. optional broad search only when the earlier tiers are insufficient.

The answer must separate measured facts, sourced macro context, conditional strategy bias,
invalidation, and missing data. The host appends every broker/news/web reference and retrieval
time. News is never treated as proof of manipulation or as a guaranteed direction.

At startup, a selected and configured Trading Economics adapter refreshes the upcoming
calendar. When a chat request contains trade intent—such as entry, long, short, position,
plan, or outlook—the agent injects nearby high-impact events and displays a pre-trade warning.
