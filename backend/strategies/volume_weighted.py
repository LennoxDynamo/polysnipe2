"""
Volume-Weighted Entry Strategy
-------------------------------
Most realistic strategy. Only enters when:
1. Price is sufficiently deviated from 0.50
2. Enough liquidity exists in the orderbook (min N× stake)
3. Spread is acceptably tight
4. BTC trend is not strongly against the trade
"""

from __base__ import BaseStrategy, Signal


class Strategy(BaseStrategy):
    NAME        = "Volume-Weighted Entry"
    DESCRIPTION = (
        "Kombiniert Preis-Abweichung mit Liquiditäts-Check. "
        "Kauft nur wenn genug Orderbook-Tiefe vorhanden ist. "
        "Realistischste Strategie — minimiert Slippage-Risiko."
    )
    AUTHOR = "PolySnipe"

    PARAMS = {
        "entry_max_price": {
            "type": "float", "default": 0.30, "min": 0.08, "max": 0.45, "step": 0.01,
            "label": "Kauf-Schwelle (max. Preis)"
        },
        "min_deviation": {
            "type": "float", "default": 0.16, "min": 0.05, "max": 0.40, "step": 0.01,
            "label": "Min. Abstand von 0.50"
        },
        "min_liquidity_multiple": {
            "type": "float", "default": 4.0, "min": 1.0, "max": 20.0, "step": 0.5,
            "label": "Min. Liquidität (× Stake)"
        },
        "max_spread": {
            "type": "float", "default": 0.04, "min": 0.01, "max": 0.12, "step": 0.005,
            "label": "Max. erlaubter Spread"
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
            "type": "int", "default": 140, "min": 30, "max": 265, "step": 5,
            "label": "Einstieg max. bis (Sekunden)"
        },
        "btc_vol_filter": {
            "type": "float", "default": 0.8, "min": 0.0, "max": 3.0, "step": 0.1,
            "label": "Max. BTC-Volatilität (0=aus)"
        },
    }

    def on_tick(self, ctx) -> Signal:
        p  = self.params
        t  = ctx.elapsed_sec
        up, dn = ctx.up_price, ctx.down_price

        if t > p["max_entry_sec"] or t >= 268:
            return Signal("SKIP", reason=f"Zeitfenster (t={t}s)")

        # BTC volatility filter
        if p["btc_vol_filter"] > 0 and ctx.btc_volatility > p["btc_vol_filter"]:
            return Signal("HOLD", reason=f"BTC-Volatilität zu hoch ({ctx.btc_volatility:.3f})")

        # Spread check
        if ctx.spread > p["max_spread"]:
            return Signal("HOLD", reason=f"Spread zu weit ({ctx.spread:.3f} > {p['max_spread']})")

        min_liq = ctx.stake * p["min_liquidity_multiple"]

        # Buy DOWN
        if dn <= p["entry_max_price"] and abs(0.5 - dn) >= p["min_deviation"]:
            if ctx.liquidity_down < min_liq:
                return Signal("HOLD",
                    reason=f"Zu wenig DOWN-Liquidität (${ctx.liquidity_down:.0f} < ${min_liq:.0f})")
            return Signal(
                "BUY", "DOWN", dn,
                reason=(
                    f"UP={up:.3f} übertrieben, liq=${ctx.liquidity_down:.0f}, "
                    f"spread={ctx.spread:.3f}, btcVol={ctx.btc_volatility:.3f}"
                ),
                confidence=self._confidence(dn),
                tp=p["take_profit"], sl=p["stop_loss"],
            )

        # Buy UP
        if up <= p["entry_max_price"] and abs(0.5 - up) >= p["min_deviation"]:
            if ctx.liquidity_up < min_liq:
                return Signal("HOLD",
                    reason=f"Zu wenig UP-Liquidität (${ctx.liquidity_up:.0f} < ${min_liq:.0f})")
            return Signal(
                "BUY", "UP", up,
                reason=(
                    f"DN={dn:.3f} übertrieben, liq=${ctx.liquidity_up:.0f}, "
                    f"spread={ctx.spread:.3f}, btcVol={ctx.btc_volatility:.3f}"
                ),
                confidence=self._confidence(up),
                tp=p["take_profit"], sl=p["stop_loss"],
            )

        return Signal("HOLD", reason=f"Keine Bedingung erfüllt: UP={up:.3f} DN={dn:.3f}")
