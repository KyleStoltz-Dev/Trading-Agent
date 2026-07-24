import asyncio
from collections.abc import Sequence

from app.market_data.cache import LiveMarketCache
from app.market_data.contracts import MarketDataConnector


async def run_quote_stream(
    connector: MarketDataConnector,
    cache: LiveMarketCache,
    instruments: Sequence[str],
    *,
    stop: asyncio.Event,
    maximum_backoff_seconds: float = 30,
) -> None:
    if not instruments:
        raise ValueError("at least one instrument is required")
    heartbeat_setter = getattr(connector, "set_heartbeat_handler", None)
    if heartbeat_setter is not None:
        heartbeat_setter(lambda at: cache.mark_heartbeat(connector.name, at))
    backoff = 0.5
    while not stop.is_set():
        cache.mark_connected(connector.name)
        try:
            async for quote in connector.stream_quotes(instruments):
                cache.put_quote(quote)
                backoff = 0.5
                if stop.is_set():
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            cache.mark_disconnected(connector.name, type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, maximum_backoff_seconds)
    cache.mark_disconnected(connector.name, "stopped")
