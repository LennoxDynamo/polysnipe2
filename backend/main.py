"""
main.py — PolySnipe Backend v2
Run locally:  uvicorn main:app --reload --port 8000
Deploy:       Procfile / Railway auto-detects uvicorn
"""

import asyncio
import csv
import io
import json
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import btc_feed
import market_data as md
import strategy_loader
from context import BtcState
from simulator import run_simulation, run_comparison

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

# ── App ──────────────────────────────────────────────────────────

app = FastAPI(
    title       = "PolySnipe API v2",
    description = "BTC 5-min Polymarket simulator backend",
    version     = "2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)

# Serve frontend
import pathlib
FRONTEND_DIR = pathlib.Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


# ── Lifecycle ────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    strategy_loader.load_all()
    asyncio.create_task(btc_feed.start())
    logger.info("PolySnipe backend started")


@app.on_event("shutdown")
async def shutdown():
    await btc_feed.stop()


# ── Request / Response models ────────────────────────────────────

class SimulateRequest(BaseModel):
    market_ids:       list[str]       = Field(..., min_length=1)
    strategy_id:      str             = Field("mean_reversion")
    strategy_params:  dict            = Field(default_factory=dict)
    initial_capital:  float           = Field(10.0,  ge=1.0,   le=10000.0)
    stake_per_trade:  float           = Field(1.0,   ge=0.1,   le=500.0)
    max_open:         int             = Field(2,      ge=1,     le=10)
    enable_slippage:  bool            = Field(True)
    market_impact:    float           = Field(0.002, ge=0.0,   le=0.05)


class CompareRequest(BaseModel):
    market_ids:      list[str]         = Field(..., min_length=1)
    strategy_ids:    list[str]         = Field(..., min_length=1)
    strategy_params: dict[str, dict]   = Field(default_factory=dict)
    initial_capital: float             = Field(10.0, ge=1.0, le=10000.0)
    stake_per_trade: float             = Field(1.0,  ge=0.1, le=500.0)
    max_open:        int               = Field(2,    ge=1,   le=10)


# ── Health ───────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    btc = BtcState.snapshot()
    return {
        "status":     "ok",
        "strategies": strategy_loader.ids(),
        "btc":        btc,
    }


# ── Strategies ───────────────────────────────────────────────────

@app.get("/api/strategies")
async def list_strategies():
    return {"strategies": strategy_loader.list_all()}


@app.get("/api/strategies/reload")
async def reload_strategies():
    count = strategy_loader.load_all()
    return {"reloaded": count, "strategies": strategy_loader.ids()}


# ── Markets ──────────────────────────────────────────────────────

@app.get("/api/markets/btc5min")
async def btc_5min(lookback: int = Query(12, ge=1, le=50)):
    try:
        markets = await md.get_btc_5min_markets(lookback)
        markets = await md.enrich_with_live_prices(markets)
        return {"markets": markets, "count": len(markets)}
    except Exception as e:
        logger.error(f"btc5min error: {e}", exc_info=True)
        raise HTTPException(500, detail=str(e))


@app.get("/api/markets/search")
async def search(q: str = Query(..., min_length=1), limit: int = Query(40, ge=1, le=100)):
    try:
        markets = await md.search_markets(q, limit)
        if markets:
            markets = await md.enrich_with_live_prices(markets)
        return {"markets": markets, "count": len(markets), "query": q}
    except Exception as e:
        logger.error(f"search error: {e}", exc_info=True)
        raise HTTPException(500, detail=str(e))


@app.get("/api/market/{market_id}")
async def get_market(market_id: str):
    market = await md.get_market_by_id(market_id)
    if not market:
        raise HTTPException(404, detail=f"Market {market_id} not found")
    ctx = await md.get_full_market_context(market)
    return {
        "market":         ctx["market"],
        "order_book_up":  ctx["ob_up"],
        "order_book_down": ctx["ob_down"],
        "liquidity_up":   ctx["liquidity_up"],
        "liquidity_down": ctx["liquidity_down"],
        "spread":         ctx["spread"],
    }


@app.get("/api/market/{market_id}/history")
async def price_history(market_id: str, ticks: int = Query(300, ge=60, le=300)):
    market = await md.get_market_by_id(market_id)
    if not market:
        raise HTTPException(404, detail=f"Market {market_id} not found")
    history = await md.build_price_history(market, ticks)
    return {"market_id": market_id, "ticks": len(history), "history": history}


# ── Context ──────────────────────────────────────────────────────

@app.get("/api/context/{market_id}")
async def market_context(market_id: str):
    """Full MarketContext snapshot for a given market."""
    market = await md.get_market_by_id(market_id)
    if not market:
        raise HTTPException(404, detail=f"Market {market_id} not found")
    ctx = await md.get_full_market_context(market)
    btc = BtcState.snapshot()
    return {
        "market_id":      market_id,
        "up_price":       ctx["market"]["up_price"],
        "down_price":     ctx["market"]["down_price"],
        "liquidity_up":   ctx["liquidity_up"],
        "liquidity_down": ctx["liquidity_down"],
        "spread":         ctx["spread"],
        "btc":            btc,
    }


# ── Signal ───────────────────────────────────────────────────────

@app.get("/api/signal/{market_id}")
async def signal(
    market_id:      str,
    strategy_id:    str   = Query("mean_reversion"),
    elapsed_sec:    int   = Query(0, ge=0, le=300),
):
    market = await md.get_market_by_id(market_id)
    if not market:
        raise HTTPException(404, detail=f"Market {market_id} not found")

    ctx_data = await md.get_full_market_context(market)
    btc      = BtcState.snapshot()
    m        = ctx_data["market"]

    from context import MarketContext
    ctx = MarketContext(
        up_price       = m["up_price"],
        down_price     = m["down_price"],
        elapsed_sec    = elapsed_sec,
        market_id      = market_id,
        btc_price      = btc["price"],
        btc_change_1m  = btc["change_1m"],
        btc_change_5m  = btc["change_5m"],
        btc_volatility = btc["volatility"],
        btc_trend      = btc["trend"],
        liquidity_up   = ctx_data["liquidity_up"],
        liquidity_down = ctx_data["liquidity_down"],
        spread         = ctx_data["spread"],
    )

    try:
        strategy = strategy_loader.get(strategy_id)
    except KeyError as e:
        raise HTTPException(400, detail=str(e))

    sig = strategy.on_tick(ctx)
    return {
        "market_id":   market_id,
        "strategy_id": strategy_id,
        "up_price":    m["up_price"],
        "down_price":  m["down_price"],
        "btc":         btc,
        "signal":      sig.as_dict(),
    }


# ── Simulate ─────────────────────────────────────────────────────

async def _fetch_market_data(market_ids: list[str]) -> list[dict]:
    async def _one(mid: str):
        market = await md.get_market_by_id(mid)
        if not market:
            return None
        ctx     = await md.get_full_market_context(market)
        history = await md.build_price_history(ctx["market"])
        return {
            "market":        ctx["market"],
            "history":       history,
            "ob_up":         ctx["ob_up"],
            "ob_down":       ctx["ob_down"],
            "liquidity_up":  ctx["liquidity_up"],
            "liquidity_down": ctx["liquidity_down"],
            "spread":        ctx["spread"],
        }
    items = await asyncio.gather(*[_one(mid) for mid in market_ids])
    return [i for i in items if i]


@app.post("/api/simulate")
async def simulate(req: SimulateRequest):
    try:
        strategy_loader.get(req.strategy_id)
    except KeyError as e:
        raise HTTPException(400, detail=str(e))

    items = await _fetch_market_data(req.market_ids)
    if not items:
        raise HTTPException(404, detail="No valid markets found")

    result = await asyncio.to_thread(
        run_simulation,
        items, req.strategy_id, req.strategy_params,
        req.initial_capital, req.stake_per_trade, req.max_open,
        req.enable_slippage, req.market_impact,
    )
    return result.as_dict()


@app.post("/api/compare")
async def compare(req: CompareRequest):
    for sid in req.strategy_ids:
        try:
            strategy_loader.get(sid)
        except KeyError as e:
            raise HTTPException(400, detail=str(e))

    items = await _fetch_market_data(req.market_ids)
    if not items:
        raise HTTPException(404, detail="No valid markets found")

    results = await asyncio.to_thread(
        run_comparison,
        items, req.strategy_ids, req.strategy_params,
        req.initial_capital, req.stake_per_trade, req.max_open,
    )
    return {"results": results}


# ── Export ───────────────────────────────────────────────────────

@app.post("/api/export/csv")
async def export_csv(req: SimulateRequest):
    """Run simulation and return results as CSV download."""
    items = await _fetch_market_data(req.market_ids)
    if not items:
        raise HTTPException(404, detail="No valid markets found")

    result = await asyncio.to_thread(
        run_simulation,
        items, req.strategy_id, req.strategy_params,
        req.initial_capital, req.stake_per_trade, req.max_open,
    )
    d = result.as_dict()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "market_id", "question", "direction",
        "entry_tick", "entry_price", "exec_price", "slippage",
        "exit_tick", "exit_price", "stake", "pnl", "pnl_pct",
        "exit_reason", "signal_reason", "confidence",
    ])
    for m in d["markets"]:
        for t in m["trades"]:
            writer.writerow([
                m["market_id"], m["question"][:60],
                t.get("direction",""), t.get("entry_tick",""),
                t.get("entry_price",""), t.get("exec_price",""),
                t.get("slippage",""), t.get("exit_tick",""),
                t.get("exit_price",""), t.get("stake",""),
                t.get("pnl",""), t.get("pnl_pct",""),
                t.get("exit_reason",""), t.get("signal_reason","")[:80],
                t.get("confidence",""),
            ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=polysnipe_trades.csv"},
    )


@app.post("/api/export/json")
async def export_json(req: SimulateRequest):
    """Run simulation and return full result as JSON download."""
    items = await _fetch_market_data(req.market_ids)
    if not items:
        raise HTTPException(404, detail="No valid markets found")

    result = await asyncio.to_thread(
        run_simulation,
        items, req.strategy_id, req.strategy_params,
        req.initial_capital, req.stake_per_trade, req.max_open,
    )
    content = json.dumps(result.as_dict(), indent=2)
    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=polysnipe_result.json"},
    )
