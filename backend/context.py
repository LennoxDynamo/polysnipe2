"""
context.py
----------
MarketContext — the single object passed to every strategy's on_tick().
Also holds the global BTC price feed (updated by btc_feed.py).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MarketContext:
    # ── Market prices ──────────────────────────────────────────
    up_price:    float = 0.5
    down_price:  float = 0.5

    # ── Timing ─────────────────────────────────────────────────
    elapsed_sec: int   = 0      # seconds since market open
    market_id:   str   = ""
    question:    str   = ""

    # ── BTC spot ───────────────────────────────────────────────
    btc_price:       float = 0.0
    btc_change_1m:   float = 0.0   # % change last 60 s
    btc_change_5m:   float = 0.0   # % change last 300 s
    btc_volatility:  float = 0.0   # rolling std-dev of 20 returns
    btc_trend:       str   = "sideways"   # "up" | "down" | "sideways"

    # ── Liquidity / orderbook ──────────────────────────────────
    liquidity_up:   float = 999.0   # $ available on UP side
    liquidity_down: float = 999.0
    spread:         float = 0.02
    volume_24h:     float = 0.0

    # ── Portfolio / position ───────────────────────────────────
    portfolio:      float = 10.0
    stake:          float = 1.0
    open_positions: list  = field(default_factory=list)

    # ── Price history (last N ticks) ───────────────────────────
    history: list = field(default_factory=list)   # [{t, up_price, dn_price}]


# ── Global BTC price state (updated by btc_feed.py) ────────────
class BtcState:
    _price_history: list[float] = []   # last 360 prices (1/s)
    _current: float = 0.0

    @classmethod
    def update(cls, price: float):
        cls._current = price
        cls._price_history.append(price)
        if len(cls._price_history) > 360:
            cls._price_history.pop(0)

    @classmethod
    def current(cls) -> float:
        return cls._current

    @classmethod
    def change_pct(cls, seconds: int) -> float:
        h = cls._price_history
        if len(h) < seconds + 1:
            return 0.0
        old = h[-seconds - 1]
        return round((h[-1] - old) / old * 100, 4) if old else 0.0

    @classmethod
    def volatility(cls, window: int = 20) -> float:
        import statistics
        h = cls._price_history
        if len(h) < window + 1:
            return 0.0
        returns = [(h[i] - h[i-1]) / h[i-1] for i in range(-window, 0) if h[i-1]]
        return round(statistics.stdev(returns) * 100, 5) if len(returns) > 1 else 0.0

    @classmethod
    def trend(cls) -> str:
        h = cls._price_history
        if len(h) < 180:
            return "sideways"
        change = cls.change_pct(180)
        if change > 0.15:
            return "up"
        if change < -0.15:
            return "down"
        return "sideways"

    @classmethod
    def snapshot(cls) -> dict:
        return {
            "price":       cls._current,
            "change_1m":   cls.change_pct(60),
            "change_5m":   cls.change_pct(300),
            "volatility":  cls.volatility(),
            "trend":       cls.trend(),
        }
