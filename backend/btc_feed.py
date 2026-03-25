"""
btc_feed.py
-----------
Connects to Binance public WebSocket and keeps BtcState updated.
Runs as a background asyncio task. Falls back to REST polling if WS fails.
"""

import asyncio
import json
import logging

import httpx

from context import BtcState

logger = logging.getLogger(__name__)

BINANCE_WS  = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_REST = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

_task: asyncio.Task | None = None


async def _ws_feed():
    """Primary: Binance WebSocket trade stream."""
    import websockets
    while True:
        try:
            logger.info("BTC feed: connecting to Binance WS...")
            async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
                logger.info("BTC feed: connected")
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
    """Fallback: poll Binance REST every 2 seconds."""
    logger.info("BTC feed: using REST fallback")
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
    """Start BTC feed. Try WS first, fall back to REST on failure."""
    global _task
    # Try WS; if environment blocks it, REST fallback will be used.
    # We run both and let WS win when available.
    try:
        import websockets  # noqa: F401
        _task = asyncio.create_task(_ws_feed())
        logger.info("BTC WebSocket feed started")
    except ImportError:
        logger.warning("websockets not installed — using REST fallback")
        _task = asyncio.create_task(_rest_fallback())


async def stop():
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
