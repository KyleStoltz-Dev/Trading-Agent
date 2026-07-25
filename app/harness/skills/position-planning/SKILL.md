---
name: position-planning
description: Turn an accepted trade thesis into deterministic risk inputs and management conditions.
triggers: risk, position size, size this, entry, stop, target, r:r, reward, management, hedge
---

Require account equity, maximum risk percentage, entry, invalidation-based stop, target, contract
specification, spread, slippage, commission, and currency conversion when applicable. Send all
arithmetic through deterministic risk code. A hedge is a separate exposure decision, not a repair
for an invalidated thesis. Never increase allowed risk because a setup feels more convincing.
