# Kyle's trading framework — working memory

This records the trader's current language and hypotheses. It is not proof of an edge. Rules should be frozen in a strategy version and evaluated through backtests and forward tests.

## Conversational behavior

- Communicate naturally and concisely.
- Use Kyle's framework rather than forcing complete ICT terminology onto it.
- State one current bias: bullish, bearish, or neutral/no trade.
- Preserve the prior bias until visible evidence changes it. Name the exact evidence when changing bias.
- Separate higher-timeframe narrative, lower-timeframe delivery, and entry confirmation.
- State what is visible, what is interpretation, what remains unconfirmed, and what invalidates the scenario.
- Reassess evidence rather than agreeing merely because Kyle suggests another reading.

## Timeframe hierarchy

- Daily and 4H: narrative, external ranges, supply/demand POIs, and major objectives.
- 1H and 15M: active delivery, pullback versus continuation, and setup POIs.
- 5M and 1M: confirmation and execution.
- A 1M shift alone is insufficient for an intraday reversal. It may execute inside a predetermined 5M/15M POI or serve as a standalone scalp/continuation toward a nearby POI.
- A trade may oppose 4H while following newly confirmed 1H/15M order flow. Treat this as countertrend with more conservative risk and targets.

## Core pattern hypothesis

Predetermined context or liquidity → accumulation/distribution → repeated drives/tests → sweep/upthrust when present → BoOF → chosen execution → predetermined target.

- Three drives improve perceived quality but are not mandatory.
- A drive is a push beyond the average consolidation area, separated by a reaction or local swing. A liquidity sweep may count as a drive.
- BoOF resembles a structural break within the controlling higher-timeframe range. Use the internal swing responsible for the final opposing push.
- Mitigation is a return to the source accumulation/distribution range or a predefined refined area.
- Imbalance means rapid one-sided repricing. A revisit is a hypothesis, not a requirement.
- Engulfing action, displacement, Fibonacci retracement, and imbalance are confluences; several can describe one event rather than independent probabilities.

## Setup families

1. POI reversal: accumulation/distribution plus BoOF inside a predetermined POI.
2. Trend continuation: reaccumulation/redistribution inside a 1H/15M/5M continuation POI.
3. Session/standalone reversal: the pattern appears at marked liquidity without a stated HTF POI; test separately and generally use lower risk.
4. Direct mitigation/rejection: first return to a valid POI followed by rejection or displacement without clean accumulation; test as a separate lower-confirmation model.

## Execution variants

Execution is separate from setup context. Compare these variants on the same qualifying pattern:

- `boof_close`: enter at the close of the candle that breaks the predetermined order-flow level.
- `push_fib_50`: first touch of the 50% retracement of the exact directional push that produced the BoOF.
- `push_fib_80`: first touch of the 80% retracement of that push.
- `schematic_fib_50`: first touch of the 50% retracement of the entire accumulation/distribution high-to-low range.
- `schematic_fib_80`: first touch of the 80% retracement of the entire schematic range.

Freeze the Fibonacci anchors and selected level before price returns. Use wick-to-wick anchors unless another strategy version explicitly tests body anchors. A missed 80% entry stays missed for that variant; do not substitute a retrospective 50% or BoOF-close fill.

For clean comparison, create a separate experiment for each execution variant. The same historical occurrence may be recorded once in each relevant experiment as a shadow execution, using the exact entry, stop, and target that variant would have produced.

## Liquidity taxonomy

Record separately: PDH/PDL, PWH/PWL, PMH/PML, Asian/London/London Close/New York highs and lows, equal highs/lows, confirmed swing highs/lows, and trendline/retail liquidity.

A sweep alone is not confirmation. Compare sweep-only, sweep plus rejection, and sweep plus accumulation/distribution and BoOF.

## Volume and time

- Fixed-range Volume Profile POC is the price with the most recorded volume for the selected range, not proof liquidity was injected there.
- Keep profile endpoints fixed because changing them changes the POC.
- Measure base duration, number of tests, width in ATR, departure speed, time until return, mitigation depth, and dwell time.
- Compare the time required to form the original move with how quickly the opposing move retraced it.

## Targets and risk

- Select targets before calculating R.
- Nearest internal liquidity is the first target; session or HTF liquidity can be later targets; distant daily/weekly levels are runner objectives.
- Entry thesis and target thesis are separate.
- Maximum account risk is 1%; lowest normal tier is 0.25%.
- Countertrend, standalone, and incomplete-confirmation setups use reduced account risk, not merely a tighter stop.
- Minimum planned reward-to-risk is 3R after realistic costs.

## Research fields

For every qualifying occurrence, including skipped trades, record setup family, direction, alignment, POI status, liquidity type, sweep, rejection, three drives, BoOF, displacement, imbalance, execution variant, Fibonacci anchor range, Fibonacci level, entry, stop, targets, planned R, realized R, MFE, MAE, session, regime, base duration, departure speed, return time, and mitigation dwell time.

Evaluate setup families and execution variants separately. Do not estimate probability during live execution; test the frozen conditions and use the predefined risk tier.
