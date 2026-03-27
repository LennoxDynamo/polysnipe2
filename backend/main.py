"""
main.py — PolySnipe Backend v2.1
Run locally:  uvicorn main:app --reload --port 8000
Deploy:       Procfile / Railway auto-detects uvicorn
"""

import asyncio
import csv
import io
import json
import logging
import os
import time
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

if __package__:
    # Package mode (Railway): uvicorn backend.main:app
    from . import btc_feed
    from . import market_data as md
    from . import strategy_loader
    from . import auth
    from . import user_store
    from .context import MarketContext, BtcState
    from .simulator import run_simulation, run_comparison
else:
    # Script mode (local from backend/): python -m uvicorn main:app
    import btc_feed
    import market_data as md
    import strategy_loader
    import auth
    import user_store
    from context import MarketContext, BtcState
    from simulator import run_simulation, run_comparison

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="PolySnipe API v2.1", version="2.1.0")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

import pathlib
FRONTEND_DIR = pathlib.Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

# ── In-memory stores ─────────────────────────────────────────────
_notes:       dict[str, list[dict]] = {}   # market_id → [{tick,text,ts}]
_price_cache: dict[str, dict]       = {}   # market_id → {up,dn,ts,token_ids,question}
_ws_clients:  set[WebSocket]        = set()
_bg_tasks:    list[asyncio.Task]    = []

# ── Lifecycle ────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    """
    Startup must return immediately so Railway healthcheck can pass.
    Background services (BTC feed, price polling) start in the background.
    """
    try:
        strategy_loader.load_all()
        logger.info(f"Loaded {len(strategy_loader.ids())} strategies")
    except Exception as e:
        logger.warning(f"Strategy loading error: {e}")
    
    # Start background tasks WITHOUT awaiting them
    # This allows the HTTP server to start and respond to /api/health immediately
    _bg_tasks.append(asyncio.create_task(btc_feed.start()))
    _bg_tasks.append(asyncio.create_task(_price_poll_loop()))
    logger.info("PolySnipe backend v2.1 started (background tasks initiated)")


@app.on_event("shutdown")
async def shutdown():
    try:
        await btc_feed.stop()
    except Exception as e:
        logger.warning(f"Shutdown error: {e}")
    # Cancel background tasks
    for task in _bg_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# ── Background price polling → push to WebSocket clients ─────────

async def _price_poll_loop():
    while True:
        # Keep WS graph/ticker updates near real-time for active markets.
        await asyncio.sleep(1)
        try:
            if not _price_cache:
                continue

            token_map: dict[str, str] = {}
            market_tokens: dict[str, list] = {}
            for mid, cached in _price_cache.items():
                tids = cached.get("token_ids", [])
                market_tokens[mid] = tids
                for tid in tids:
                    token_map[tid] = mid

            all_tokens = list(token_map.keys())
            if not all_tokens:
                continue

            live = await md.get_live_prices(all_tokens)
            updates = []
            for mid, tids in market_tokens.items():
                if tids and tids[0] in live:
                    up_p = round(live[tids[0]], 4)
                    dn_p = round(live.get(tids[1], 1 - up_p), 4) if len(tids) > 1 else round(1 - up_p, 4)
                    _price_cache[mid].update({"up": up_p, "dn": dn_p, "ts": time.time()})
                    updates.append({
                        "type": "price", "market_id": mid,
                        "up_price": up_p, "down_price": dn_p, "ts": time.time(),
                    })

            if updates and _ws_clients:
                payload = json.dumps({"prices": updates, "btc": BtcState.snapshot()})
                dead = set()
                for ws in _ws_clients:
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        dead.add(ws)
                _ws_clients -= dead

        except Exception as e:
            logger.debug(f"price poll: {e}")


# ── Request models ────────────────────────────────────────────────

class GoogleLoginRequest(BaseModel):
    id_token: str = Field(..., description="Google ID token from frontend")

class AuthResponse(BaseModel):
    token: str
    user_id: str
    identity_type: str
    email: Optional[str] = None

class SettingsUpdateRequest(BaseModel):
    settings: dict = Field(default_factory=dict, description="User settings dict to persist")

class SimulateRequest(BaseModel):
    market_ids:      list[str] = Field(..., min_length=1)
    strategy_id:     str       = Field("mean_reversion")
    strategy_params: dict      = Field(default_factory=dict)
    initial_capital: float     = Field(10.0,  ge=1.0,   le=10000.0)
    stake_per_trade: float     = Field(1.0,   ge=0.1,   le=500.0)
    max_open:        int       = Field(2,      ge=1,     le=10)
    enable_slippage: bool      = Field(True)
    market_impact:   float     = Field(0.002, ge=0.0,   le=0.05)

class CompareRequest(BaseModel):
    market_ids:      list[str]       = Field(..., min_length=1)
    strategy_ids:    list[str]       = Field(..., min_length=1)
    strategy_params: dict[str, dict] = Field(default_factory=dict)
    initial_capital: float           = Field(10.0, ge=1.0, le=10000.0)
    stake_per_trade: float           = Field(1.0,  ge=0.1, le=500.0)
    max_open:        int             = Field(2,    ge=1,   le=10)

class NoteRequest(BaseModel):
    tick: int = Field(..., ge=0, le=300)
    text: str = Field(..., min_length=1, max_length=200)

# ── Health ────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {
        "status": "ok", "version": "2.1.0",
        "strategies": strategy_loader.ids(),
        "btc": BtcState.snapshot(),
        "tracked_markets": len(_price_cache),
    }


@app.get("/api/config")
async def get_config():
    """
    Get frontend configuration (public safe values).
    Used by frontend to initialize Google Sign-In and other settings.
    """
    return {
        "google_client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "api_version": "2.1.0",
    }


# ── WebSocket ────────────────────────────────────────────────────

@app.websocket("/ws/prices")
async def ws_prices(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    logger.info(f"WS client connected ({len(_ws_clients)} total)")
    try:
        # Immediate snapshot
        snapshot = {
            "prices": [
                {"type": "price", "market_id": mid,
                 "up_price": v["up"], "down_price": v["dn"], "ts": v["ts"]}
                for mid, v in _price_cache.items()
            ],
            "btc": BtcState.snapshot(),
        }
        await ws.send_text(json.dumps(snapshot))
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"pong": True, "btc": BtcState.snapshot()}))
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


# ── Track markets for live polling ───────────────────────────────

@app.post("/api/track")
async def track_markets(body: dict):
    for mid in body.get("market_ids", []):
        if mid not in _price_cache:
            market = await md.get_market_by_id(mid)
            if market:
                _price_cache[mid] = {
                    "up": market["up_price"], "dn": market["down_price"],
                    "ts": time.time(), "token_ids": market.get("token_ids", []),
                    "question": market["question"],
                }
    return {"tracked": list(_price_cache.keys())}


@app.delete("/api/track/{market_id}")
async def untrack_market(market_id: str):
    _price_cache.pop(market_id, None)
    return {"ok": True}


@app.get("/api/prices")
async def get_prices_rest(market_ids: str = Query(...)):
    ids = [x.strip() for x in market_ids.split(",") if x.strip()]
    return {
        "prices": {
            mid: {"up_price": _price_cache[mid]["up"],
                  "down_price": _price_cache[mid]["dn"],
                  "ts": _price_cache[mid]["ts"]}
            for mid in ids if mid in _price_cache
        },
        "btc": BtcState.snapshot(),
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
        for m in markets:
            if m.get("active") and not m.get("closed") and m["id"] not in _price_cache:
                _price_cache[m["id"]] = {
                    "up": m["up_price"], "dn": m["down_price"],
                    "ts": time.time(), "token_ids": m.get("token_ids", []),
                    "question": m["question"],
                }
        return {"markets": markets, "count": len(markets)}
    except Exception as e:
        logger.error(f"btc5min: {e}", exc_info=True)
        raise HTTPException(500, detail=str(e))


@app.get("/api/markets/search")
async def search(q: str = Query(..., min_length=1), limit: int = Query(40, ge=1, le=100)):
    try:
        markets = await md.search_markets(q, limit)
        if markets:
            markets = await md.enrich_with_live_prices(markets)
        return {"markets": markets, "count": len(markets), "query": q}
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.get("/api/market/{market_id}")
async def get_market(market_id: str):
    market = await md.get_market_by_id(market_id)
    if not market:
        raise HTTPException(404, detail=f"Market {market_id} not found")
    ctx = await md.get_full_market_context(market)
    return {
        "market": ctx["market"],
        "order_book_up": ctx["ob_up"], "order_book_down": ctx["ob_down"],
        "liquidity_up": ctx["liquidity_up"], "liquidity_down": ctx["liquidity_down"],
        "spread": ctx["spread"],
    }


@app.get("/api/market/{market_id}/history")
async def price_history(market_id: str, ticks: int = Query(300, ge=60, le=300)):
    market = await md.get_market_by_id(market_id)
    if not market:
        raise HTTPException(404, detail=f"Market {market_id} not found")
    history = await md.build_price_history(market, ticks)
    return {"market_id": market_id, "ticks": len(history), "history": history}


@app.get("/api/context/{market_id}")
async def market_context(market_id: str):
    market = await md.get_market_by_id(market_id)
    if not market:
        raise HTTPException(404, detail=f"Market {market_id} not found")
    ctx = await md.get_full_market_context(market)
    return {
        "market_id": market_id,
        "up_price": ctx["market"]["up_price"], "down_price": ctx["market"]["down_price"],
        "liquidity_up": ctx["liquidity_up"], "liquidity_down": ctx["liquidity_down"],
        "spread": ctx["spread"], "btc": BtcState.snapshot(),
    }


@app.get("/api/signal/{market_id}")
async def signal(
    market_id:   str,
    strategy_id: str = Query("mean_reversion"),
    elapsed_sec: int = Query(0, ge=0, le=300),
):
    market = await md.get_market_by_id(market_id)
    if not market:
        raise HTTPException(404, detail=f"Market {market_id} not found")
    ctx_data = await md.get_full_market_context(market)
    btc = BtcState.snapshot()
    m   = ctx_data["market"]
    ctx = MarketContext(
        up_price=m["up_price"], down_price=m["down_price"],
        elapsed_sec=elapsed_sec, market_id=market_id,
        btc_price=btc["price"], btc_change_1m=btc["change_1m"],
        btc_change_5m=btc["change_5m"], btc_volatility=btc["volatility"],
        btc_trend=btc["trend"], liquidity_up=ctx_data["liquidity_up"],
        liquidity_down=ctx_data["liquidity_down"], spread=ctx_data["spread"],
    )
    try:
        strategy = strategy_loader.get(strategy_id)
    except KeyError as e:
        raise HTTPException(400, detail=str(e))
    sig = strategy.on_tick(ctx)
    return {"market_id": market_id, "strategy_id": strategy_id,
            "up_price": m["up_price"], "down_price": m["down_price"],
            "btc": btc, "signal": sig.as_dict()}


# ── Notes ────────────────────────────────────────────────────────

@app.get("/api/notes/{market_id}")
async def get_notes(market_id: str):
    return {"market_id": market_id, "notes": _notes.get(market_id, [])}

@app.post("/api/notes/{market_id}")
async def add_note(market_id: str, req: NoteRequest):
    _notes.setdefault(market_id, []).append(
        {"tick": req.tick, "text": req.text, "ts": time.time()}
    )
    return {"ok": True}

@app.delete("/api/notes/{market_id}")
async def clear_notes(market_id: str):
    _notes.pop(market_id, None)
    return {"ok": True}


# ── Auth ──────────────────────────────────────────────────────────

@app.post("/api/auth/google-login")
async def google_login(req: GoogleLoginRequest):
    """
    Google Sign-In endpoint.
    Verify the ID token and issue a JWT.
    """
    try:
        google_client_id = os.getenv("GOOGLE_CLIENT_ID")
        # Verify token from Google
        payload = auth.verify_google_token(req.id_token, google_client_id)
        user_id = payload.get("sub")
        email = payload.get("email")
        
        if not user_id or not email:
            raise HTTPException(400, detail="Invalid Google token: missing sub or email")
        
        # Issue JWT for authenticated user
        token = auth.create_jwt(user_id, auth.IdentityType.GOOGLE, email=email)
        
        # Load user's existing settings if any
        settings = await user_store.load_settings(user_id)
        
        return AuthResponse(
            token=token, user_id=user_id,
            identity_type=auth.IdentityType.GOOGLE, email=email
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Google login error: {e}")
        raise HTTPException(401, detail="Google authentication failed")


@app.post("/api/auth/guest-login")
async def guest_login():
    """
    Guest login (no account needed).
    Issue an ephemeral guest JWT that expires in 30 days.
    """
    try:
        guest_id = str(uuid.uuid4())
        token = auth.create_jwt(guest_id, auth.IdentityType.GUEST)
        
        return AuthResponse(
            token=token, user_id=guest_id,
            identity_type=auth.IdentityType.GUEST, email=None
        )
    except Exception as e:
        logger.warning(f"Guest login error: {e}")
        raise HTTPException(500, detail="Guest login failed")


@app.get("/api/auth/me")
async def get_current_user_info(authorization: Optional[str] = Header(None, alias="Authorization")):
    """
    Get current authenticated user identity from JWT.
    Validates token and returns user info.
    """
    try:
        user = auth.get_current_user(authorization)
        return {
            "user_id": user["user_id"],
            "identity_type": user["identity_type"],
            "email": user.get("email"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Auth validation error: {e}")
        raise HTTPException(401, detail="Unauthorized")


@app.post("/api/auth/logout")
async def logout(authorization: Optional[str] = Header(None, alias="Authorization")):
    """
    Logout endpoint (stateless; client discards token).
    """
    try:
        user = auth.get_current_user(authorization)
        logger.info(f"User logged out: {user['user_id']}")
        return {"ok": True, "message": "Logged out successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Logout error: {e}")
        return {"ok": False}


@app.get("/api/auth/settings")
async def get_user_settings(authorization: Optional[str] = Header(None, alias="Authorization")):
    """
    Get authenticated user's settings.
    Guests should not call this; they use localStorage.
    """
    try:
        user = auth.get_current_user(authorization)
        if user["identity_type"] == auth.IdentityType.GUEST:
            # Guests store settings client-side; return empty dict
            return {"settings": {}}
        
        user_id = user["user_id"]
        settings = await user_store.load_settings(user_id)
        return {"settings": settings}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Get settings error: {e}")
        raise HTTPException(401, detail="Unauthorized")


@app.post("/api/auth/settings")
async def save_user_settings(req: SettingsUpdateRequest, authorization: Optional[str] = Header(None, alias="Authorization")):
    """
    Save authenticated user's settings server-side.
    Guests should not call this; they use localStorage.
    """
    try:
        user = auth.get_current_user(authorization)
        if user["identity_type"] == auth.IdentityType.GUEST:
            # Guests cannot save server-side; they should use localStorage
            return {"ok": False, "message": "Guest users save settings client-side"}
        
        user_id = user["user_id"]
        success = await user_store.save_settings(user_id, req.settings)
        
        if not success:
            logger.warning(f"Failed to save settings for user {user_id}")
            return {"ok": False, "message": "Could not save settings"}
        
        return {"ok": True, "message": "Settings saved"}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Save settings error: {e}")
        raise HTTPException(401, detail="Unauthorized")


# ── Simulate ─────────────────────────────────────────────────────

async def _fetch_market_data(market_ids: list[str]) -> list[dict]:
    async def _one(mid):
        market = await md.get_market_by_id(mid)
        if not market:
            return None
        ctx     = await md.get_full_market_context(market)
        history = await md.build_price_history(ctx["market"])
        return {
            "market": ctx["market"], "history": history,
            "ob_up": ctx["ob_up"], "ob_down": ctx["ob_down"],
            "liquidity_up": ctx["liquidity_up"],
            "liquidity_down": ctx["liquidity_down"],
            "spread": ctx["spread"],
        }
    items = await asyncio.gather(*[_one(m) for m in market_ids])
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
        run_simulation, items, req.strategy_id, req.strategy_params,
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
        run_comparison, items, req.strategy_ids, req.strategy_params,
        req.initial_capital, req.stake_per_trade, req.max_open,
    )
    return {"results": results}


# ── Export ────────────────────────────────────────────────────────

@app.post("/api/export/csv")
async def export_csv(req: SimulateRequest):
    items = await _fetch_market_data(req.market_ids)
    if not items:
        raise HTTPException(404, detail="No valid markets found")
    result = await asyncio.to_thread(
        run_simulation, items, req.strategy_id, req.strategy_params,
        req.initial_capital, req.stake_per_trade, req.max_open,
    )
    d = result.as_dict()
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["market_id","question","direction","entry_tick","entry_price",
                "exec_price","slippage","exit_tick","exit_price","stake",
                "pnl","pnl_pct","exit_reason","signal_reason","confidence"])
    for m in d["markets"]:
        for t in m["trades"]:
            w.writerow([m["market_id"], m["question"][:60],
                t.get("direction",""), t.get("entry_tick",""), t.get("entry_price",""),
                t.get("exec_price",""), t.get("slippage",""), t.get("exit_tick",""),
                t.get("exit_price",""), t.get("stake",""), t.get("pnl",""),
                t.get("pnl_pct",""), t.get("exit_reason",""),
                t.get("signal_reason","")[:80], t.get("confidence","")])
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=polysnipe_trades.csv"})


@app.post("/api/export/json")
async def export_json(req: SimulateRequest):
    items = await _fetch_market_data(req.market_ids)
    if not items:
        raise HTTPException(404, detail="No valid markets found")
    result = await asyncio.to_thread(
        run_simulation, items, req.strategy_id, req.strategy_params,
        req.initial_capital, req.stake_per_trade, req.max_open,
    )
    content = json.dumps(result.as_dict(), indent=2)
    return StreamingResponse(iter([content]), media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=polysnipe_result.json"})
