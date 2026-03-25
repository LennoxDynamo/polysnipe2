"""
Momentum Fade Strategy
----------------------
Detects short-term price momentum within a market and fades it.
If UP price has risen sharply in the last N ticks → buy DOWN.
If UP price has fallen sharply in the last N ticks → buy UP.
Also uses BTC context: avoids fading moves aligned with BTC trend.
"""

from __base__ import BaseStrategy, Signal


class Strategy(BaseStrategy):
    NAME        = "Momentum Fade"
    DESCRIPTION = (
        "Kauft gegen kurzfristige Preisbewegungen. Wenn der UP-Preis "
        "schnell steigt, kauft der Algo DOWN — er wettet auf eine Korrektur. "
        "Nutzt BTC-Kontext: vermeidet Fades die mit dem BTC-Trend übereinstimmen."
    )
    AUTHOR = "PolySnipe"

    PARAMS = {
        "momentum_window": {
            "type": "int", "default": 12, "min": 5, "max": 40, "step": 1,
            "label": "Momentum-Fenster (Ticks)"
        },
        "momentum_threshold": {
            "type": "float", "default": 0.07, "min": 0.03, "max": 0.20, "step": 0.01,
            "label": "Min. Preisbewegung für Signal"
        },
        "price_cap": {
            "type": "float", "default": 0.35, "min": 0.10, "max": 0.48, "step": 0.01,
            "label": "Max. Kaufpreis"
        },
        "take_profit": {
            "type": "float", "default": 0.42, "min": 0.20, "max": 0.49, "step": 0.01,
            "label": "Take Profit"
        },
        "stop_loss": {
            "type": "float", "default": 0.14, "min": 0.03, "max": 0.28, "step": 0.01,
            "label": "Stop Loss"
        },
        "max_entry_sec": {
            "type": "int", "default": 180, "min": 30, "max": 265, "step": 5,
            "label": "Einstieg max. bis (Sekunden)"
        },
        "btc_filter": {
            "type": "bool", "default": True,
            "label": "BTC-Trendfilter aktivieren"
        },
    }

    def on_tick(self, ctx) -> Signal:
        p   = self.params
        t   = ctx.elapsed_sec
        h   = ctx.history
        win = p["momentum_window"]

        if t > p["max_entry_sec"] or t >= 268:
            return Signal("SKIP", reason=f"Zeitfenster (t={t}s)")

        if len(h) < win + 1:
            return Signal("HOLD", reason="Nicht genug History")

        price_now  = h[-1]["up_price"]
        price_then = h[-(win + 1)]["up_price"]
        move       = price_now - price_then   # positive = UP moved up

        # BTC trend filter: don't fade moves aligned with BTC
        if p["btc_filter"]:
            btc_trend = ctx.btc_trend
            if btc_trend == "up"   and move > 0:
                return Signal("HOLD", reason=f"BTC-Trend 'up' — kein DOWN-Fade")
            if btc_trend == "down" and move < 0:
                return Signal("HOLD", reason=f"BTC-Trend 'down' — kein UP-Fade")

        # Fade upward momentum → buy DOWN
        if move >= p["momentum_threshold"] and ctx.down_price <= p["price_cap"]:
            return Signal(
                "BUY", "DOWN", ctx.down_price,
                reason=f"UP +{move:.3f} in {win}s — fade down (BTC:{ctx.btc_trend})",
                confidence=min(1.0, move / 0.15),
                tp=p["take_profit"], sl=p["stop_loss"],
            )

        # Fade downward momentum → buy UP
        if move <= -p["momentum_threshold"] and ctx.up_price <= p["price_cap"]:
            return Signal(
                "BUY", "UP", ctx.up_price,
                reason=f"UP {move:.3f} in {win}s — fade up (BTC:{ctx.btc_trend})",
                confidence=min(1.0, abs(move) / 0.15),
                tp=p["take_profit"], sl=p["stop_loss"],
            )

        return Signal("HOLD", reason=f"Momentum {move:+.3f} unter Schwelle {p['momentum_threshold']}")
