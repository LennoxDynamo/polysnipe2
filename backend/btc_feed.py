"""
btc_feed.py
-----------
Connects to Binance public WebSocket and keeps BtcState updated.

Both WS and REST run in parallel — WS wins when available (updates are more
frequent), REST fills the gap when WS is blocked or slow. Both call
BtcState.update() which is idempotent, so parallel writes are safe.
"""

import asyncio
import json
import logging

import httpx

try:
    from .context import BtcState
except ImportError:
    from context import BtcState

logger = logging.getLogger(__name__)

BINANCE_WS   = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_REST = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

_tasks: list[asyncio.Task] = []


async def _ws_feed():
    """Primary: Binance WebSocket trade stream. Reconnects automatically."""
    import websockets
    while True:
        try:
            logger.info("BTC feed: connecting to Binance WS...")
            async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
                logger.info("BTC feed: WS connected")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        price = float(msg.get("p", 0))
                        if price > 0:
                            BtcState.update(price)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"BTC feed WS error: {e} — retrying in 5s")
            await asyncio.sleep(5)


async def _rest_fallback():
    """
    Always-on REST fallback: polls Binance every 2s.
    Runs alongside WS — updates BtcState with the same call,
    so if WS is blocked on a host (e.g. Railway proxy), REST keeps prices live.
    """
    logger.info("BTC feed: REST fallback started (runs in parallel with WS)")
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                r = await client.get(BINANCE_REST)
                if r.status_code == 200:
                    price = float(r.json().get("price", 0))
                    if price > 0:
                        BtcState.update(price)
            except Exception as e:
                logger.debug(f"BTC REST poll error: {e}")
            await asyncio.sleep(2)


async def start():
    """
    Start both WS and REST feeds in parallel.
    WS provides near-real-time updates (~ms latency).
    REST provides a 2s fallback if WS is unavailable.
    Both are always running — BtcState.update() is idempotent.
    """
    global _tasks
    _tasks = []

    try:
        import websockets  # noqa: F401
        _tasks.append(asyncio.create_task(_ws_feed()))
        logger.info("BTC WebSocket feed started")
    except ImportError:
        logger.warning("websockets not installed — WS feed skipped")

    # REST fallback always starts regardless of WS availability
    _tasks.append(asyncio.create_task(_rest_fallback()))
    logger.info("BTC REST fallback feed started")


async def stop():
    global _tasks
    for task in _tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _tasks = []