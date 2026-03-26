"""
simulator.py
------------
Tick-by-tick backtest engine.
Supports multiple strategies, realistic slippage model,
market impact, and per-trade detail logging.

Changes vs v1:
- TP/SL read from Trade object (set at entry from Signal), not from strategy.params at exit
- Resolution logic distinguishes closed (binary 0/1) vs open (exit at current price) markets
"""

from __future__ import annotations

import contextlib
import io
import uuid
from dataclasses import dataclass, field
from typing import Optional

try:
    from . import strategy_loader
    from .context import MarketContext, BtcState
    from .market_data import _compute_slippage_price
except ImportError:
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
    entry_price:   float
    exec_price:    float
    slippage:      float
    stake:         float
    shares:        float

    # TP/SL stored per-trade from the Signal that generated this entry
    tp:            float = 0.38
    sl:            float = 0.12

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
            "tp":           self.tp,
            "sl":           self.sl,
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
    debug_log:    list[dict]   = field(default_factory=list)

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
            "debug_log":    self.debug_log,
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

def _resolve_exit_price(trade: Trade, market: dict, up_price: float, dn_price: float) -> float:
    """
    Determine the exit price when a market reaches its end (tick 299).

    - Closed/resolved market → binary 1.0 (win) or 0.0 (loss)
    - Active/unresolved market → exit at current live price (no premature binary assumption)
    """
    is_closed = market.get("closed", False)
    target_up = market.get("up_price", 0.5)

    if is_closed:
        # Market has resolved — pay out at 1.0 or 0.0
        won = (
            (trade.direction == "UP"   and target_up > 0.5) or
            (trade.direction == "DOWN" and target_up < 0.5)
        )
        return 1.0 if won else 0.0
    else:
        # Market still open — close at current tick price
        return up_price if trade.direction == "UP" else dn_price


def run_simulation(
    markets_with_data:  list[dict],
    strategy_id:        str,
    strategy_params:    dict  = {},
    initial_capital:    float = 10.0,
    stake_per_trade:    float = 1.0,
    max_open:           int   = 2,
    enable_slippage:    bool  = True,
    market_impact:      float = 0.002,
) -> SimResult:

    strategy = strategy_loader.get(strategy_id, strategy_params)
    strategy.reset()

    portfolio = initial_capital
    result    = SimResult(
        strategy_id    = strategy_id,
        strategy_name  = strategy.NAME,
        initial_capital = initial_capital,
    )

    def _run_with_stdout(fn, *args, _debug_sink=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = fn(*args)
        if _debug_sink:
            for line in buf.getvalue().splitlines():
                msg = line.strip()
                if msg:
                    _debug_sink(msg, source="print")
        return out

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
        current_tick = 0

        def _append_debug(message: str, source: str = "debug"):
            mkt_result.debug_log.append({
                "tick": max(0, int(current_tick)),
                "source": source,
                "message": str(message)[:500],
            })

        if hasattr(strategy, "set_debug_sink"):
            strategy.set_debug_sink(lambda msg: _append_debug(msg, source="debug"))

        _run_with_stdout(strategy.on_market_start, MarketContext(
            market_id=mkt_id, question=question,
            portfolio=portfolio, stake=stake_per_trade,
        ), _debug_sink=_append_debug)

        def _liq_at(t: int, total_liq: float) -> float:
            progress = t / 300
            import math
            factor = math.exp(-((progress - 0.5) ** 2) / (2 * 0.18 ** 2))
            return max(20.0, total_liq * factor)

        def _exec_price(direction: str, stake: float, t: int) -> tuple[float, float]:
            if not enable_slippage:
                ref = history[t]["up_price"] if direction == "UP" else history[t]["dn_price"]
                return ref, 0.0

            liq_for_side = _liq_at(t, liq_down if direction == "DOWN" else liq_up)
            ref_price    = history[t]["dn_price"] if direction == "DOWN" else history[t]["up_price"]

            if ob_up and direction == "UP" and ob_up.get("asks"):
                exec_p = _compute_slippage_price(ob_up["asks"], stake)
            elif ob_down and direction == "DOWN" and ob_down.get("asks"):
                exec_p = _compute_slippage_price(ob_down["asks"], stake)
            else:
                slip   = ref_price * (stake / max(liq_for_side, 1.0)) * 0.5
                exec_p = min(0.95, ref_price + slip)

            slippage = round(exec_p - ref_price, 5)
            return round(exec_p, 5), slippage

        for tick_data in history:
            t        = tick_data["t"]
            current_tick = t
            up_price = tick_data["up_price"]
            dn_price = tick_data["dn_price"]
            price_window.append(tick_data)

            cur_liq_up   = _liq_at(t, liq_up)
            cur_liq_down = _liq_at(t, liq_down)

            # ── Check exits — use per-trade TP/SL from Signal ────
            for tid, trade in list(open_trades.items()):
                cur = up_price if trade.direction == "UP" else dn_price

                if cur >= trade.tp:
                    trade.close(cur, t, f"TP ({cur:.3f}≥{trade.tp})")
                elif cur <= trade.sl:
                    trade.close(cur, t, f"SL ({cur:.3f}≤{trade.sl})")
                elif t >= 299:
                    exit_price = _resolve_exit_price(trade, market, up_price, dn_price)
                    reason = "Auflösung (resolved)" if market.get("closed") else "Marktende (offen)"
                    trade.close(exit_price, t, reason)
                else:
                    continue

                portfolio += trade.shares * trade.exit_price
                mkt_result.trades.append(trade)
                _run_with_stdout(strategy.on_close, trade.as_dict(), _debug_sink=_append_debug)
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
                sig = _run_with_stdout(strategy.on_tick, ctx, _debug_sink=_append_debug)

                if sig.action == "BUY" and sig.direction not in already_dirs:
                    exec_p, slippage = _exec_price(sig.direction, stake_per_trade, t)

                    # Use TP/SL from signal, fall back to strategy params
                    trade_tp = sig.tp if sig.tp is not None else strategy.params.get("take_profit", 0.38)
                    trade_sl = sig.sl if sig.sl is not None else strategy.params.get("stop_loss",   0.12)

                    if exec_p < trade_tp:
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
                            tp           = trade_tp,
                            sl           = trade_sl,
                            signal_reason = sig.reason,
                            confidence    = sig.confidence,
                        )
                        open_trades[tid] = trade

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

        # Force-close remaining open positions
        for tid, trade in list(open_trades.items()):
            last = history[-1] if history else {}
            up_price = last.get("up_price", market.get("up_price", 0.5))
            dn_price = last.get("dn_price", 1 - up_price)
            exit_price = _resolve_exit_price(trade, market, up_price, dn_price)
            reason = "Auflösung (resolved)" if market.get("closed") else "Marktende (offen)"
            trade.close(exit_price, 299, reason)
            portfolio += trade.shares * exit_price
            mkt_result.trades.append(trade)
            _run_with_stdout(strategy.on_close, trade.as_dict(), _debug_sink=_append_debug)

        if hasattr(strategy, "set_debug_sink"):
            strategy.set_debug_sink(None)

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