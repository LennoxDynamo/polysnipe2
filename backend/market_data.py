"""
market_data.py
--------------
All Polymarket data access:
  - Gamma API  → market search, metadata, slug lookup
  - py-clob-client → live midpoint prices, order books, trade history
"""

import asyncio
import json
import logging
import time
from typing import Optional

import httpx
from fastapi import HTTPException
from py_clob_client.client import ClobClient

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"

_clob: Optional[ClobClient] = None


def get_clob() -> ClobClient:
    global _clob
    if _clob is None:
        _clob = ClobClient(CLOB_BASE)
    return _clob


# ── Gamma API ────────────────────────────────────────────────────

async def gamma_get(path: str, params: dict = None) -> list | dict:
    async with httpx.AsyncClient(timeout=12) as client:
        try:
            r = await client.get(f"{GAMMA_BASE}{path}", params=params or {})
        except httpx.ConnectError as e:
            logger.error(f"Gamma API connection error: {e}")
            raise HTTPException(503, detail="Gamma API nicht erreichbar — Verbindungsfehler")
        except httpx.TimeoutException:
            logger.error(f"Gamma API timeout for {path}")
            raise HTTPException(504, detail="Gamma API Timeout — bitte erneut versuchen")

        if r.status_code == 429:
            retry = r.headers.get("Retry-After", "30")
            logger.warning(f"Gamma API rate limit hit, retry-after: {retry}s")
            raise HTTPException(429, detail=f"Polymarket Rate Limit — bitte {retry}s warten")
        if r.status_code == 404:
            raise HTTPException(404, detail=f"Markt nicht gefunden: {path}")
        if r.status_code >= 500:
            logger.error(f"Gamma API server error {r.status_code} for {path}")
            raise HTTPException(502, detail=f"Polymarket Server-Fehler ({r.status_code})")

        r.raise_for_status()
        return r.json()


async def search_markets(query: str, limit: int = 40) -> list[dict]:
    data = await gamma_get("/markets", params={
        "limit": limit, "active": "true", "closed": "false",
        "_textSearch": query,
    })
    return _normalize_markets(data if isinstance(data, list) else data.get("data", []))


async def get_btc_5min_markets(lookback_windows: int = 12) -> list[dict]:
    now  = int(time.time())
    base = (now // 300) * 300
    slugs = [f"btc-updown-5m-{base + i * 300}" for i in range(0, -lookback_windows, -1)]

    async with httpx.AsyncClient(timeout=12) as client:
        tasks = [client.get(f"{GAMMA_BASE}/markets", params={"slug": s}) for s in slugs]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    markets, seen = [], set()
    for resp in responses:
        if isinstance(resp, Exception):
            logger.debug(f"BTC 5min slug fetch error: {resp}")
            continue
        if isinstance(resp, httpx.Response) and resp.status_code == 429:
            logger.warning("Rate limit hit during BTC 5min slug fetch")
            continue
        try:
            data = resp.json()
            for m in (data if isinstance(data, list) else []):
                if m.get("id") and m["id"] not in seen:
                    seen.add(m["id"]); markets.append(m)
        except Exception as e:
            logger.debug(f"BTC 5min parse error: {e}")
            continue
    return _normalize_markets(markets)


async def get_market_by_id(market_id: str) -> Optional[dict]:
    try:
        data = await gamma_get(f"/markets/{market_id}")
        if isinstance(data, list):
            data = data[0] if data else None
        return _normalize_market(data) if data else None
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"get_market_by_id({market_id}): {e}")
        return None


# ── CLOB (sync → thread pool) ────────────────────────────────────

def _clob_midpoints(token_ids: list[str]) -> dict[str, float]:
    client = get_clob()
    try:
        result = client.get_midpoints(token_ids)
        mid_map = result.get("mid", result) if isinstance(result, dict) else {}
        parsed = {k: float(v) for k, v in mid_map.items() if v is not None}
        if parsed:
            return parsed
    except Exception as e:
        logger.debug(f"get_midpoints batch error: {e}")

    out: dict[str, float] = {}
    for tid in token_ids:
        try:
            one = client.get_midpoint(tid)
            if isinstance(one, dict):
                val = one.get("mid")
            else:
                val = one
            if val is None:
                last = client.get_last_trade_price(tid)
                if isinstance(last, dict):
                    val = last.get("price")
                else:
                    val = last
            if val is not None:
                out[tid] = float(val)
        except Exception as e:
            logger.debug(f"get_midpoint({tid}) error: {e}")
    return out


def _clob_order_book(token_id: str) -> Optional[dict]:
    try:
        book = get_clob().get_order_book(token_id)
        if not book:
            return None
        bids = sorted([(float(b.price), float(b.size)) for b in (book.bids or [])], reverse=True)
        asks = sorted([(float(a.price), float(a.size)) for a in (book.asks or [])])
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        spread   = round(best_ask - best_bid, 4) if best_bid and best_ask else None

        liq_bid = sum(p * s for p, s in bids[:10])
        liq_ask = sum(p * s for p, s in asks[:10])

        return {
            "bids": bids[:10], "asks": asks[:10],
            "best_bid": best_bid, "best_ask": best_ask,
            "spread": spread,
            "liquidity_bid": round(liq_bid, 2),
            "liquidity_ask": round(liq_ask, 2),
        }
    except Exception as e:
        logger.debug(f"get_order_book({token_id}): {e}")
        return None


def _compute_slippage_price(book_levels: list[tuple], stake: float) -> float:
    if not book_levels:
        return book_levels[0][0] if book_levels else 0.5

    remaining = stake
    total_cost = 0.0
    total_shares = 0.0

    for price, size in book_levels:
        available_dollars = price * size
        take = min(remaining, available_dollars)
        shares = take / price
        total_cost   += take
        total_shares += shares
        remaining    -= take
        if remaining <= 0:
            break

    if total_shares == 0:
        return book_levels[0][0]
    return round(total_cost / total_shares, 5)


def _clob_trades(token_id: str, limit: int = 200) -> list[dict]:
    try:
        trades = get_clob().get_trades(params={"market": token_id, "limit": limit})
        if isinstance(trades, dict):
            trades = trades.get("data", [])
        return trades or []
    except Exception as e:
        logger.debug(f"get_trades({token_id}): {e}")
        return []


# ── Async wrappers ───────────────────────────────────────────────

async def get_live_prices(token_ids: list[str]) -> dict[str, float]:
    return await asyncio.to_thread(_clob_midpoints, token_ids)


async def get_order_book(token_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_clob_order_book, token_id)


async def get_trade_history(token_id: str, limit: int = 200) -> list[dict]:
    return await asyncio.to_thread(_clob_trades, token_id, limit)


# ── Enrichment ───────────────────────────────────────────────────

async def enrich_with_live_prices(markets: list[dict]) -> list[dict]:
    all_tokens = [t for m in markets for t in m.get("token_ids", [])]
    if not all_tokens:
        return markets
    live = await get_live_prices(all_tokens)
    for m in markets:
        tids = m.get("token_ids", [])
        if len(tids) >= 1:
            up_p = live.get(tids[0])
            if up_p is not None:
                m["up_price"]    = round(up_p, 4)
                m["down_price"]  = round(live.get(tids[1], 1 - up_p), 4)
                m["price_source"] = "clob_live"
    return markets


async def get_full_market_context(market: dict) -> dict:
    tids = market.get("token_ids", [])
    ob_up = ob_dn = None

    if len(tids) >= 2:
        ob_up, ob_dn = await asyncio.gather(
            get_order_book(tids[0]),
            get_order_book(tids[1]),
        )
        live = await get_live_prices(tids)
        if live.get(tids[0]):
            market["up_price"]   = round(live[tids[0]], 4)
            market["down_price"] = round(live.get(tids[1], 1 - market["up_price"]), 4)
            market["price_source"] = "clob_live"

    return {
        "market":    market,
        "ob_up":     ob_up,
        "ob_down":   ob_dn,
        "liquidity_up":   ob_up["liquidity_ask"] if ob_up else 500.0,
        "liquidity_down": ob_dn["liquidity_ask"] if ob_dn else 500.0,
        "spread":    ob_up["spread"] if ob_up else 0.02,
    }


# ── Price history for replay ─────────────────────────────────────

async def build_price_history(market: dict, ticks: int = 300) -> list[dict]:
    tids     = market.get("token_ids", [])
    up_token = tids[0] if tids else None
    live_up  = market.get("up_price", 0.5)
    is_closed = market.get("closed", False)

    real_trades: list[dict] = []
    if up_token:
        real_trades = await get_trade_history(up_token, limit=300)

    if real_trades:
        return _trades_to_ticks(real_trades, ticks, live_up, is_closed)
    return _live_price_walk(live_up, ticks, is_closed)


def _trades_to_ticks(trades: list, ticks: int, live_up: float, is_closed: bool) -> list[dict]:
    sorted_t = sorted(trades, key=lambda t: t.get("timestamp", t.get("matchTime", 0)))
    prices = []
    for t in sorted_t:
        try:
            p = float(t.get("price", t.get("tradePrice", 0)))
            if 0 < p < 1:
                prices.append(p)
        except (ValueError, TypeError):
            continue

    if len(prices) < 3:
        return _synthetic_walk(live_up, ticks, is_closed)

    step    = max(1, len(prices) // ticks)
    sampled = [prices[min(i * step, len(prices) - 1)] for i in range(ticks)]
    return [{"t": i, "up_price": round(max(0.02, min(0.98, v)), 4),
             "dn_price": round(1 - max(0.02, min(0.98, v)), 4)}
            for i, v in enumerate(sampled)]


def _live_price_walk(live_up: float, ticks: int = 300, resolved: bool = False) -> list[dict]:
    import random
    random.seed(int(live_up * 1e6) ^ ticks)

    start_offset = (random.random() - 0.5) * 0.04
    up = max(0.02, min(0.98, live_up + start_offset))

    history = []
    for t in range(ticks):
        progress = t / ticks
        pull = (live_up - up) * (0.005 + progress * 0.02)
        volatility = 0.008 * (1 - progress * 0.3)
        noise = (random.random() - 0.5) * volatility
        spike = (random.random() - 0.5) * 0.04 if random.random() < 0.08 else 0
        up = max(0.02, min(0.98, up + pull + noise + spike))
        history.append({"t": t, "up_price": round(up, 4), "dn_price": round(1 - up, 4)})

    history[-1].update({"up_price": round(live_up, 4), "dn_price": round(1 - live_up, 4)})
    return history


def _synthetic_walk(target_up: float, ticks: int = 300, resolved: bool = False) -> list[dict]:
    import random
    random.seed(int(target_up * 1e6) ^ ticks)
    up = 0.5
    history = []
    for t in range(ticks):
        progress = t / ticks
        pull  = (target_up - up) * (0.05 if resolved else 0.018) * (1 + progress * 3)
        noise = (random.random() - 0.5) * max(0.004, 0.024 - progress * 0.014)
        spike = (random.random() - 0.5) * 0.13 if random.random() < 0.012 else 0
        up    = max(0.02, min(0.98, up + pull + noise + spike))
        if resolved and t >= 278:
            up = up + (target_up - up) * 0.22
        history.append({"t": t, "up_price": round(up, 4), "dn_price": round(1 - up, 4)})
    history[-1].update({"up_price": round(target_up, 4), "dn_price": round(1 - target_up, 4)})
    return history


# ── Normalizers ──────────────────────────────────────────────────

def _normalize_markets(raw: list) -> list[dict]:
    return [m for m in (_normalize_market(r) for r in raw) if m]


def _normalize_market(m: dict) -> Optional[dict]:
    if not m or not isinstance(m, dict):
        return None
    raw_prices = m.get("outcomePrices", "[]")
    try:
        prices = (raw_prices if isinstance(raw_prices, list)
                  else json.loads(raw_prices))
        prices = [float(p) for p in prices]
    except Exception:
        prices = [0.5, 0.5]

    raw_tokens = m.get("clobTokenIds", "[]")
    try:
        token_ids = (raw_tokens if isinstance(raw_tokens, list)
                     else json.loads(raw_tokens))
    except Exception:
        token_ids = []

    up_p = prices[0] if prices else 0.5
    dn_p = prices[1] if len(prices) > 1 else (1 - up_p)

    end_date     = m.get("endDate") or m.get("endDateIso")
    seconds_left = None
    if end_date:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            seconds_left = max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
        except Exception:
            pass

    return {
        "id":           m.get("id", ""),
        "slug":         m.get("slug", ""),
        "question":     m.get("question", m.get("title", "")),
        "active":       bool(m.get("active", False)),
        "closed":       bool(m.get("closed", False)),
        "up_price":     round(up_p, 4),
        "down_price":   round(dn_p, 4),
        "token_ids":    token_ids,
        "volume":       float(m.get("volume",    0) or 0),
        "volume_24h":   float(m.get("volume24hr", 0) or 0),
        "liquidity":    float(m.get("liquidity",  0) or 0),
        "end_date":     end_date,
        "seconds_left": seconds_left,
        "price_source": "gamma",
        "description":  m.get("description", ""),
    }