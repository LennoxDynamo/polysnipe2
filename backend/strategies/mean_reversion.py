"""
Mean Reversion Strategy
-----------------------
Buys the cheaper side when the market overreacts in either direction.
Classic baseline: if UP is at 0.75, the market has overreacted upward
so we buy DOWN (at 0.25) expecting a revert toward 0.50.
"""

from __base__ import BaseStrategy, Signal


class Strategy(BaseStrategy):
    NAME        = "Mean Reversion"
    DESCRIPTION = (
        "Kauft die günstigere Seite wenn der Markt stark von 50/50 abweicht. "
        "Geht davon aus, dass extreme Ausschläge sich korrigieren. "
        "Einfachster Algorithmus — guter Baseline."
    )
    AUTHOR = "PolySnipe"

    PARAMS = {
        "entry_max_price": {
            "type": "float", "default": 0.28, "min": 0.08, "max": 0.45, "step": 0.01,
            "label": "Kauf-Schwelle (max. Preis)"
        },
        "min_deviation": {
            "type": "float", "default": 0.18, "min": 0.05, "max": 0.40, "step": 0.01,
            "label": "Min. Abstand von 0.50"
        },
        "take_profit": {
            "type": "float", "default": 0.38, "min": 0.20, "max": 0.49, "step": 0.01,
            "label": "Take Profit"
        },
        "stop_loss": {
            "type": "float", "default": 0.12, "min": 0.03, "max": 0.25, "step": 0.01,
            "label": "Stop Loss"
        },
        "max_entry_sec": {
            "type": "int", "default": 150, "min": 30, "max": 265, "step": 5,
            "label": "Einstieg max. bis (Sekunden)"
        },
    }

    def on_tick(self, ctx) -> Signal:
        p = self.params
        up, dn = ctx.up_price, ctx.down_price
        t = ctx.elapsed_sec

        # Timing guard
        if t > p["max_entry_sec"] or t >= 268:
            return Signal("SKIP", reason=f"Zeitfenster (t={t}s)")

        # Buy DOWN: market has overreacted upward
        if dn <= p["entry_max_price"] and abs(0.5 - dn) >= p["min_deviation"]:
            return Signal(
                "BUY", "DOWN", dn,
                reason=f"UP übertrieben ({up:.3f}), DOWN günstig ({dn:.3f})",
                confidence=self._confidence(dn),
                tp=p["take_profit"], sl=p["stop_loss"],
            )

        # Buy UP: market has overreacted downward
        if up <= p["entry_max_price"] and abs(0.5 - up) >= p["min_deviation"]:
            return Signal(
                "BUY", "UP", up,
                reason=f"DOWN übertrieben ({dn:.3f}), UP günstig ({up:.3f})",
                confidence=self._confidence(up),
                tp=p["take_profit"], sl=p["stop_loss"],
            )

        return Signal("HOLD", reason=f"Keine Überreaktion: UP={up:.3f} DN={dn:.3f}")
