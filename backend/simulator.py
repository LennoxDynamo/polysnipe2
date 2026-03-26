"""
simulator.py
------------
Tick-by-tick backtest engine.
Supports multiple strategies, realistic slippage model,
market impact, and per-trade detail logging.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

import strategy_loader
from context import MarketContext, BtcState
from market_data import _compute_slippage_price


# ── Trade / Result models ────────────────────────────────────────

@dataclass
class Trade:
    id:            str
    market_id:     str
    direction:     str
    entry_tick:    int
    entry_price:   float          # requested price
    exec_price:    float          # after slippage
    slippage:      float          # exec_price - entry_price
    stake:         float
    shares:        float          # stake / exec_price

    exit_tick:     Optional[int]   = None
    exit_price:    Optional[float] = None
    pnl:           Optional[float] = None
    pnl_pct:       Optional[float] = None
    exit_reason:   Optional[str]   = None
    signal_reason: str             = ""
    confidence:    float           = 0.0

    def close(self, price: float, tick: int, reason: str):
        self.exit_tick   = tick
        self.exit_price  = price
        payout           = self.shares * price
        self.pnl         = round(payout - self.stake, 6)
        self.pnl_pct     = round(self.pnl / self.stake * 100, 3)
        self.exit_reason = reason

    def as_dict(self) -> dict:
        return {
            "id":           self.id,
            "market_id":    self.market_id,
            "direction":    self.direction,
            "entry_tick":   self.entry_tick,
            "entry_price":  self.entry_price,
            "exec_price":   self.exec_price,
            "slippage":     self.slippage,
            "stake":        self.stake,
            "exit_tick":    self.exit_tick,
            "exit_price":   self.exit_price,
            "pnl":          self.pnl,
            "pnl_pct":      self.pnl_pct,
            "exit_reason":  self.exit_reason,
            "signal_reason": self.signal_reason,
            "confidence":   self.confidence,
        }


@dataclass
class MarketResult:
    market_id:    str
    question:     str
    strategy_id:  str
    active:       bool = False
    closed:       bool = False
    seconds_left: Optional[int] = None
    trades:       list[Trade]  = field(default_factory=list)
    equity_curve: list[float]  = field(default_factory=list)
    price_curve:  list[dict]   = field(default_factory=list)

    @property
    def pnl(self) -> float:
        return sum(t.pnl for t in self.trades if t.pnl is not None)

    @property
    def avg_slippage(self) -> float:
        slips = [t.slippage for t in self.trades]
        return round(sum(slips) / len(slips), 5) if slips else 0.0

    def as_dict(self) -> dict:
        return {
            "market_id":    self.market_id,
            "question":     self.question,
            "strategy_id":  self.strategy_id,
            "active":       self.active,
            "closed":       self.closed,
            "seconds_left": self.seconds_left,
            "pnl":          round(self.pnl, 5),
            "avg_slippage": self.avg_slippage,
            "trades":       [t.as_dict() for t in self.trades],
            "equity_curve": self.equity_curve,
            "price_curve":  self.price_curve,
        }


@dataclass
class SimResult:
    strategy_id:     str
    strategy_name:   str
    initial_capital: float
    markets:         list[MarketResult] = field(default_factory=list)

    @property
    def final_capital(self) -> float:
        return self.initial_capital + self.total_pnl

    @property
    def total_pnl(self) -> float:
        return sum(m.pnl for m in self.markets)

    @property
    def total_trades(self) -> int:
        return sum(len(m.trades) for m in self.markets)

    @property
    def wins(self) -> int:
        return sum(1 for m in self.markets for t in m.trades if t.pnl and t.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for m in self.markets for t in m.trades if t.pnl and t.pnl <= 0)

    @property
    def win_rate(self) -> Optional[float]:
        t = self.total_trades
        return round(self.wins / t * 100, 1) if t else None

    @property
    def max_drawdown(self) -> float:
        curve = [v for m in self.markets for v in m.equity_curve]
        if len(curve) < 2:
            return 0.0
        peak, max_dd = curve[0], 0.0
        for v in curve:
            peak  = max(peak, v)
            dd    = (peak - v) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        return round(max_dd * 100, 2)

    @property
    def avg_slippage(self) -> float:
        slips = [t.slippage for m in self.markets for t in m.trades]
        return round(sum(slips) / len(slips), 5) if slips else 0.0

    def as_dict(self) -> dict:
        return {
            "strategy_id":    self.strategy_id,
            "strategy_name":  self.strategy_name,
            "initial_capital": self.initial_capital,
            "final_capital":  round(self.final_capital, 4),
            "total_pnl":      round(self.total_pnl, 5),
            "total_pnl_pct":  round(self.total_pnl / self.initial_capital * 100, 2)
                              if self.initial_capital else 0,
            "total_trades":   self.total_trades,
            "wins":           self.wins,
            "losses":         self.losses,
            "win_rate":       self.win_rate,
            "max_drawdown_pct": self.max_drawdown,
            "avg_slippage":   self.avg_slippage,
            "markets_count":  len(self.markets),
            "markets":        [m.as_dict() for m in self.markets],
        }


# ── Engine ───────────────────────────────────────────────────────

def run_simulation(
    markets_with_data:  list[dict],
    strategy_id:        str,
    strategy_params:    dict  = {},
    initial_capital:    float = 10.0,
    stake_per_trade:    float = 1.0,
    max_open:           int   = 2,
    enable_slippage:    bool  = True,
    market_impact:      float = 0.002,   # fraction of stake/liquidity
) -> SimResult:

    strategy = strategy_loader.get(strategy_id, strategy_params)
    strategy.reset()

    portfolio = initial_capital
    result    = SimResult(
        strategy_id    = strategy_id,
        strategy_name  = strategy.NAME,
        initial_capital = initial_capital,
    )

    for entry in markets_with_data:
        market   = entry["market"]
        history  = entry["history"]
        ob_up    = entry.get("ob_up")
        ob_down  = entry.get("ob_down")
        liq_up   = entry.get("liquidity_up",   500.0)
        liq_down = entry.get("liquidity_down", 500.0)
        spread   = entry.get("spread",          0.02)

        mkt_id   = market.get("id", str(uuid.uuid4()))
        question = market.get("question", "")

        mkt_result  = MarketResult(
            market_id=mkt_id,
            question=question,
            strategy_id=strategy_id,
            active=bool(market.get("active", False)),
            closed=bool(market.get("closed", False)),
            seconds_left=market.get("seconds_left"),
        )
        open_trades: dict[str, Trade] = {}
        price_window: list[dict] = []

        strategy.on_market_start(MarketContext(
            market_id=mkt_id, question=question,
            portfolio=portfolio, stake=stake_per_trade,
        ))

        # Simulate time-varying liquidity: low at start, peaks at ~50%, low at end
        def _liq_at(t: int, total_liq: float) -> float:
            progress = t / 300
            # Bell curve: peaks at t=150
            import math
            factor = math.exp(-((progress - 0.5) ** 2) / (2 * 0.18 ** 2))
            return max(20.0, total_liq * factor)

        # Compute slippage price using orderbook or fallback
        def _exec_price(direction: str, stake: float, t: int) -> tuple[float, float]:
            """Returns (exec_price, slippage)."""
            if not enable_slippage:
                ref = history[t]["up_price"] if direction == "UP" else history[t]["dn_price"]
                return ref, 0.0

            # Use orderbook if available, else linear approximation
            liq_for_side = _liq_at(t, liq_down if direction == "DOWN" else liq_up)
            ref_price    = history[t]["dn_price"] if direction == "DOWN" else history[t]["up_price"]

            if ob_up and direction == "UP" and ob_up.get("asks"):
                exec_p = _compute_slippage_price(ob_up["asks"], stake)
            elif ob_down and direction == "DOWN" and ob_down.get("asks"):
                exec_p = _compute_slippage_price(ob_down["asks"], stake)
            else:
                # Linear slippage: stake/liquidity × price
                slip   = ref_price * (stake / max(liq_for_side, 1.0)) * 0.5
                exec_p = min(0.95, ref_price + slip)

            slippage = round(exec_p - ref_price, 5)
            return round(exec_p, 5), slippage

        for tick_data in history:
            t        = tick_data["t"]
            up_price = tick_data["up_price"]
            dn_price = tick_data["dn_price"]
            price_window.append(tick_data)

            cur_liq_up   = _liq_at(t, liq_up)
            cur_liq_down = _liq_at(t, liq_down)

            # ── Check exits ──────────────────────────────────────
            for tid, trade in list(open_trades.items()):
                cur = up_price if trade.direction == "UP" else dn_price
                sig_tp = strategy.params.get("take_profit", 0.38)
                sig_sl = strategy.params.get("stop_loss",   0.12)

                if cur >= sig_tp:
                    trade.close(cur, t, f"TP ({cur:.3f}≥{sig_tp})")
                elif cur <= sig_sl:
                    trade.close(cur, t, f"SL ({cur:.3f}≤{sig_sl})")
                elif t >= 299:
                    target_up = market.get("up_price", 0.5)
                    resolved  = 1.0 if (
                        (trade.direction == "UP"   and target_up > 0.5) or
                        (trade.direction == "DOWN" and target_up < 0.5)
                    ) else 0.0
                    trade.close(resolved, t, "Auflösung")
                else:
                    continue

                portfolio += trade.shares * trade.exit_price
                mkt_result.trades.append(trade)
                strategy.on_close(trade.as_dict())
                del open_trades[tid]

            # ── Build context ────────────────────────────────────
            ctx = MarketContext(
                up_price       = up_price,
                down_price     = dn_price,
                elapsed_sec    = t,
                market_id      = mkt_id,
                question       = question,
                btc_price      = BtcState.current(),
                btc_change_1m  = BtcState.change_pct(60),
                btc_change_5m  = BtcState.change_pct(300),
                btc_volatility = BtcState.volatility(),
                btc_trend      = BtcState.trend(),
                liquidity_up   = cur_liq_up,
                liquidity_down = cur_liq_down,
                spread         = spread,
                portfolio      = portfolio,
                stake          = stake_per_trade,
                open_positions = [tr.as_dict() for tr in open_trades.values()],
                history        = price_window[-60:],
            )

            # ── Entry signal ──────────────────────────────────────
            already_dirs = {tr.direction for tr in open_trades.values()}
            if len(open_trades) < max_open and portfolio >= stake_per_trade:
                sig = strategy.on_tick(ctx)

                if sig.action == "BUY" and sig.direction not in already_dirs:
                    exec_p, slippage = _exec_price(sig.direction, stake_per_trade, t)

                    # Reject if slippage blows past our TP
                    if exec_p < (sig.tp or 1.0):
                        portfolio -= stake_per_trade
                        tid        = str(uuid.uuid4())[:8]
                        trade      = Trade(
                            id           = tid,
                            market_id    = mkt_id,
                            direction    = sig.direction,
                            entry_tick   = t,
                            entry_price  = sig.price or exec_p,
                            exec_price   = exec_p,
                            slippage     = slippage,
                            stake        = stake_per_trade,
                            shares       = stake_per_trade / exec_p,
                            signal_reason = sig.reason,
                            confidence    = sig.confidence,
                        )
                        open_trades[tid] = trade

                        # Apply market impact: shift price slightly
                        if enable_slippage and market_impact > 0:
                            impact = (stake_per_trade / max(
                                cur_liq_down if sig.direction == "DOWN" else cur_liq_up, 1.0
                            )) * market_impact
                            if sig.direction == "DOWN":
                                history[t]["dn_price"] = round(
                                    min(0.98, dn_price + impact), 4)
                            else:
                                history[t]["up_price"] = round(
                                    min(0.98, up_price + impact), 4)

            mkt_result.equity_curve.append(round(portfolio, 4))
            mkt_result.price_curve.append({
                "t": t, "up_price": up_price, "dn_price": dn_price,
            })

        # Force-close remaining
        for tid, trade in list(open_trades.items()):
            target_up = market.get("up_price", 0.5)
            resolved  = 1.0 if (
                (trade.direction == "UP"   and target_up > 0.5) or
                (trade.direction == "DOWN" and target_up < 0.5)
            ) else 0.0
            trade.close(resolved, 299, "Auflösung")
            portfolio += trade.shares * resolved
            mkt_result.trades.append(trade)
            strategy.on_close(trade.as_dict())

        result.markets.append(mkt_result)

    return result


# ── Multi-strategy comparison ─────────────────────────────────────

def run_comparison(
    markets_with_data: list[dict],
    strategy_ids:      list[str],
    strategy_params:   dict[str, dict] = {},
    initial_capital:   float = 10.0,
    stake_per_trade:   float = 1.0,
    max_open:          int   = 2,
) -> list[dict]:
    results = []
    for sid in strategy_ids:
        params = strategy_params.get(sid, {})
        result = run_simulation(
            markets_with_data = markets_with_data,
            strategy_id       = sid,
            strategy_params   = params,
            initial_capital   = initial_capital,
            stake_per_trade   = stake_per_trade,
            max_open          = max_open,
        )
        results.append(result.as_dict())
    return results
