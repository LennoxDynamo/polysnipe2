"""
strategies/__base__.py
----------------------
Base class every strategy must inherit from.
The plugin loader (strategy_loader.py) scans /strategies/ for files
containing a class named `Strategy(BaseStrategy)`.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Literal


@dataclass
class Signal:
    action:     Literal["BUY", "HOLD", "SKIP", "CLOSE"]
    direction:  Optional[Literal["UP", "DOWN"]] = None
    price:      Optional[float] = None
    reason:     str = ""
    confidence: float = 0.0
    tp:         Optional[float] = None
    sl:         Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "action":     self.action,
            "direction":  self.direction,
            "price":      self.price,
            "reason":     self.reason,
            "confidence": round(self.confidence, 3),
            "tp":         self.tp,
            "sl":         self.sl,
        }


class BaseStrategy:
    # ── Metadata (override in subclass) ──────────────────────────
    NAME        = "Unnamed Strategy"
    DESCRIPTION = ""
    AUTHOR      = ""

    # ── Parameter schema ─────────────────────────────────────────
    # Format: { param_name: { type, default, min, max, step, label } }
    # The frontend auto-generates sliders from this schema.
    PARAMS: dict = {}

    def __init__(self, params: dict = {}):
        # Merge provided params with defaults
        self.params = {
            k: v["default"]
            for k, v in self.PARAMS.items()
        }
        self.params.update({
            k: v for k, v in params.items()
            if k in self.PARAMS
        })

    # ── Required ─────────────────────────────────────────────────
    def on_tick(self, ctx) -> Signal:
        """
        Called on every price tick during simulation.
        ctx: MarketContext — see context.py for all available fields.
        Return a Signal with action BUY / HOLD / SKIP.
        """
        raise NotImplementedError

    # ── Optional ─────────────────────────────────────────────────
    def on_close(self, trade: dict) -> None:
        """
        Called when a position is closed.
        Useful for adaptive strategies that learn from past trades.
        trade: { direction, entry_price, exit_price, pnl, pnl_pct, exit_reason }
        """
        pass

    def on_market_start(self, ctx) -> None:
        """Called at the start of each new market (t=0)."""
        pass

    def reset(self) -> None:
        """Reset any internal state (called before each backtest run)."""
        pass

    # ── Helpers available to all strategies ──────────────────────
    def _confidence(self, buy_price: float) -> float:
        """Qualitative confidence 0–1 based on distance from 0.50."""
        entry_max = self.params.get("entry_max_price", 0.30)
        dev = abs(0.5 - buy_price)
        min_dev = self.params.get("min_deviation", 0.15)
        price_score = max(0, (entry_max - buy_price) / entry_max)
        dev_score   = max(0, (dev - min_dev) / (0.5 - min_dev))
        return round(min(1.0, (price_score + dev_score) / 2), 3)

    def meta(self) -> dict:
        return {
            "id":          self.__class__.__module__,
            "name":        self.NAME,
            "description": self.DESCRIPTION,
            "author":      self.AUTHOR,
            "params":      self.PARAMS,
            "current_params": self.params,
        }
