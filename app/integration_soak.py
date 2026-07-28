"""Bounded deterministic integration soak test.

Run with:
    python -m app.integration_soak --iterations 100 --max-seconds 30

This module never contacts real providers and never proves production reachability.
"""

import argparse
import asyncio
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date

from app.services.integration_simulator import DeterministicIntegrationSimulator
from app.services.observability import JsonEventSink, event_json


@dataclass(frozen=True, slots=True)
class SoakResult:
    requested_iterations: int
    completed_iterations: int
    checks: int
    failures: int
    elapsed_seconds: float
    bounded_by_time: bool
    verification_source: str = "simulated"


async def run_soak(
    *,
    iterations: int = 100,
    max_seconds: float = 30,
    fault_every: int = 0,
    sink: JsonEventSink | None = None,
) -> SoakResult:
    if not 1 <= iterations <= 10_000:
        raise ValueError("iterations must be between 1 and 10000")
    if not 0.1 <= max_seconds <= 300:
        raise ValueError("max_seconds must be between 0.1 and 300")
    if not 0 <= fault_every <= iterations:
        raise ValueError("fault_every must be zero or no greater than iterations")
    simulator = DeterministicIntegrationSimulator()
    started = time.monotonic()
    completed = 0
    checks = 0
    failures = 0
    bounded_by_time = False
    if sink is not None:
        sink.emit(
            "integration.soak.started",
            component="integration-soak",
            outcome="running",
            fields={
                "iterations": iterations,
                "max_seconds": max_seconds,
                "verification_source": "simulated",
            },
        )
    try:
        for index in range(iterations):
            if time.monotonic() - started >= max_seconds:
                bounded_by_time = True
                break
            if fault_every and (index + 1) % fault_every == 0:
                simulator.inject(
                    "oanda",
                    f"/v3/accounts/{simulator.account_id}/summary",
                    ("429",),
                )
                simulator.inject(
                    "metatrader",
                    "/v1/health",
                    ("reset",),
                )
                simulator.inject(
                    "trading-economics",
                    "/news",
                    ("timeout",),
                )
            oanda = simulator.oanda()
            metatrader = simulator.metatrader()
            news = simulator.trading_economics()
            operations = (
                oanda.account(),
                oanda.positions(),
                oanda.latest_quote("XAU_USD"),
                oanda.candles("XAU_USD", "M5", count=1),
                oanda.events_since("0"),
                metatrader.health(),
                metatrader.account(),
                metatrader.positions(),
                metatrader.latest_quote("XAUUSD"),
                metatrader.candles("XAUUSD", "M5", count=1),
                metatrader.events_since(None),
                news.calendar(
                    start=date(2026, 7, 27),
                    end=date(2026, 7, 27),
                    countries=("United States",),
                    minimum_importance=2,
                ),
                news.news(limit=1),
                simulator.tradingview(
                    event_id=f"soak-{index}",
                    secret=simulator.webhook_secret,
                ),
            )
            remaining = max_seconds - (time.monotonic() - started)
            if remaining <= 0:
                bounded_by_time = True
                for operation in operations:
                    operation.close()
                break
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*operations, return_exceptions=True),
                    timeout=remaining,
                )
            except TimeoutError:
                checks += len(operations)
                failures += len(operations)
                bounded_by_time = True
                break
            checks += len(results)
            iteration_failures = sum(
                isinstance(result, BaseException) for result in results
            )
            failures += iteration_failures
            completed += 1
            if iteration_failures and sink is not None:
                sink.emit(
                    "integration.soak.iteration",
                    component="integration-soak",
                    outcome="failed",
                    fields={
                        "iteration": index + 1,
                        "failure_count": iteration_failures,
                    },
                )
    finally:
        await simulator.aclose()
    result = SoakResult(
        requested_iterations=iterations,
        completed_iterations=completed,
        checks=checks,
        failures=failures,
        elapsed_seconds=round(time.monotonic() - started, 6),
        bounded_by_time=bounded_by_time,
    )
    if sink is not None:
        sink.emit(
            "integration.soak.completed",
            component="integration-soak",
            outcome="passed" if failures == 0 else "failed",
            fields=asdict(result),
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run bounded simulated integration checks. This never verifies real "
            "credentials, terminals, or public HTTPS."
        )
    )
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--max-seconds", type=float, default=30)
    parser.add_argument(
        "--fault-every",
        type=int,
        default=0,
        help="Inject recoverable 429/reset/timeout faults every N iterations.",
    )
    arguments = parser.parse_args(argv)
    sink = JsonEventSink(lambda line: print(line, flush=True))
    try:
        result = asyncio.run(
            run_soak(
                iterations=arguments.iterations,
                max_seconds=arguments.max_seconds,
                fault_every=arguments.fault_every,
                sink=sink,
            )
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(event_json({"summary": asdict(result)}))
    return 0 if result.failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
