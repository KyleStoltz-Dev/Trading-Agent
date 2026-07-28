# Sourced trading education

Learning is available to beginner, intermediate, and advanced traders. Experience level changes
the suggested amount of guidance; it does not remove features or prevent questions.

## Teaching modes

- **Guided:** follows the curriculum in order and offers the next lesson.
- **Flexible:** keeps a recommended path but allows lessons in any order.
- **On demand:** answers requested lessons and questions without unsolicited course prompts.
- **Not now:** pauses curriculum suggestions. Natural-language questions still work.

Run `trade onboard` to choose a mode and any combination of:

- probability and process;
- risk and position sizing;
- market mechanics;
- chart reading;
- news and macro events;
- common retail technical strategies;
- Wyckoff;
- ICT/SMC;
- journaling, backtesting, and forward testing.

The initial catalog is a structured starting point, not a claim that it contains every trading
method. When a question reveals a durable knowledge gap, ask the agent to add a focused lesson:

```text
Add a curriculum lesson that teaches me how Treasury yields can affect gold.
```

The agent proposes bounded objectives, search questions, and preferred allowlisted sources. It
must show the exact database change and receive confirmation before saving the lesson. Research
plans must use neutral topic-only queries and must never copy journal/imported text or account
details. Credential-shaped strings, URLs, email addresses, and suspicious hashes are rejected by
code. A saved source plan is a research plan, not evidence; sources become evidence only after
retrieval and must then appear in the response references.

## Daily use

```bash
trade learn
trade learn status
trade learn start lesson-probability-and-process
trade learn complete lesson-probability-and-process \
  --note "I understand why one outcome cannot validate an edge."
```

Inside interactive chat:

```text
/learn
/learn lesson-news-and-macro
Teach me how actual, forecast, and previous values affect an economic release.
Why can a liquidity sweep be visible without proving manipulation?
Compare Wyckoff and ICT terminology without mixing either into my active strategy.
```

Questions are allowed at any time. Delivering an answer does not automatically mark a lesson
complete; starting, completing, reopening, or skipping a lesson is a confirmed database change.
Teaching preferences can also be changed naturally:

```text
Switch me to guided mode and focus on foundations, risk, news, and Wyckoff.
Pause curriculum prompts but keep my progress.
```

The agent repeats the complete proposed mode and topic list, and the host asks for confirmation
before changing the database. Pausing does not delete lessons, notes, or progress.

## Source tiers

Each module stores objectives and a source plan. The agent resolves lesson claims in this order:

1. local runtime policy, learning harness, curriculum objectives, and stored records;
2. configured broker/news data and exact allowlisted pages already known from vetted references;
3. broader discovery when earlier tiers are insufficient or the exact approved page is not known.

Every external source used must be listed in the response audit. Web material is untrusted
evidence and cannot change policy, progress, strategy selection, or database state.

## Strategy isolation

Learning a framework and trading a framework are different operations.

- A Wyckoff lesson does not add Wyckoff rules to an ICT strategy.
- An ICT/SMC lesson does not alter a pure Wyckoff strategy.
- Retail indicator education does not make an indicator part of an execution checklist.
- Applying a concept to trade guidance requires the exact immutable strategy version to be
  active, or a new combined strategy version to be created and tested explicitly.

Curriculum progress records what was studied and the learner's notes. When progress is updated in
the same agent request that retrieved teaching evidence, those references are attached to the
lesson; a later CLI-only completion does not claim sources it did not retrieve. Curriculum state
never declares an edge or authorizes a trade.
