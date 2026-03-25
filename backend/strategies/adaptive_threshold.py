"""
Adaptive Threshold Strategy
----------------------------
Starts with a base entry threshold and tightens it after consecutive losses,
loosens it after wins. Uses on_close() callback to learn from trade history.
Also applies a dynamic timing multiplier based on market age.
"""

from __base__ import BaseStrategy, Signal


class Strategy(BaseStrategy):
    NAME        = "Adaptive Threshold"
    DESCRIPTION = (
        "Passt den Kauf-Schwellenwert dynamisch an. Nach Verlusten wird er "
        "strenger, nach Gewinnen lockerer. Zeitbasiert: in den letzten 60s "
        "wird die Schwelle automatisch verschärft."
    )
    AUTHOR = "PolySnipe"

    PARAMS = {
        "base_entry_price": {
            "type": "float", "default": 0.28, "min": 0.10, "max": 0.42, "step": 0.01,
            "label": "Basis-Kaufschwelle"
        },
        "adapt_step": {
            "type": "float", "default": 0.02, "min": 0.005, "max": 0.05, "step": 0.005,
            "label": "Anpassungsschritt pro Trade"
        },
        "adapt_min": {
            "type": "float", "default": 0.14, "min": 0.05, "max": 0.25, "step": 0.01,
            "label": "Minimum-Schwelle"
        },
        "adapt_max": {
            "type": "float", "default": 0.36, "min": 0.25, "max": 0.45, "step": 0.01,
            "label": "Maximum-Schwelle"
        },
        "min_deviation": {
            "type": "float", "default": 0.16, "min": 0.05, "max": 0.40, "step": 0.01,
            "label": "Min. Abstand von 0.50"
        },
        "take_profit": {
            "type": "float", "default": 0.40, "min": 0.20, "max": 0.49, "step": 0.01,
            "label": "Take Profit"
        },
        "stop_loss": {
            "type": "float", "default": 0.13, "min": 0.03, "max": 0.25, "step": 0.01,
            "label": "Stop Loss"
        },
        "max_entry_sec": {
            "type": "int", "default": 150, "min": 30, "max": 265, "step": 5,
            "label": "Einstieg max. bis (Sekunden)"
        },
    }

    def __init__(self, params={}):
        super().__init__(params)
        self._current_threshold = self.params["base_entry_price"]
        self._trade_count = 0
        self._recent_results = []   # last 5 trade pnls

    def reset(self):
        self._current_threshold = self.params["base_entry_price"]
        self._trade_count = 0
        self._recent_results = []

    def on_close(self, trade: dict):
        """Adjust threshold based on trade outcome."""
        pnl = trade.get("pnl", 0)
        self._recent_results.append(pnl)
        if len(self._recent_results) > 5:
            self._recent_results.pop(0)
        self._trade_count += 1

        step = self.params["adapt_step"]
        if pnl < 0:
            # Loss → tighten threshold (require cheaper entry)
            self._current_threshold = max(
                self.params["adapt_min"],
                self._current_threshold - step
            )
        else:
            # Win → can relax slightly
            self._current_threshold = min(
                self.params["adapt_max"],
                self._current_threshold + step * 0.5
            )

    def on_tick(self, ctx) -> Signal:
        p  = self.params
        t  = ctx.elapsed_sec
        up, dn = ctx.up_price, ctx.down_price

        # Time-decay: tighten threshold in last 60s
        time_penalty = 0.0
        if t > 240:
            time_penalty = (t - 240) / 300 * 0.06
        effective_threshold = max(p["adapt_min"], self._current_threshold - time_penalty)

        if t > p["max_entry_sec"] or t >= 268:
            return Signal("SKIP", reason=f"Zeitfenster (t={t}s)")

        reason_suffix = (
            f"[adaptive thr={effective_threshold:.3f}, "
            f"trades={self._trade_count}, "
            f"recent={'%.3f' % self._recent_results[-1] if self._recent_results else 'n/a'}]"
        )

        if dn <= effective_threshold and abs(0.5 - dn) >= p["min_deviation"]:
            return Signal(
                "BUY", "DOWN", dn,
                reason=f"UP={up:.3f} übertrieben {reason_suffix}",
                confidence=self._confidence(dn),
                tp=p["take_profit"], sl=p["stop_loss"],
            )

        if up <= effective_threshold and abs(0.5 - up) >= p["min_deviation"]:
            return Signal(
                "BUY", "UP", up,
                reason=f"DN={dn:.3f} übertrieben {reason_suffix}",
                confidence=self._confidence(up),
                tp=p["take_profit"], sl=p["stop_loss"],
            )

        return Signal("HOLD", reason=f"Kein Signal (eff. Schwelle={effective_threshold:.3f})")
