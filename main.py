"""
TradeBot - Servidor principal
Recebe webhooks do TradingView, executa ordens na Kraken, serve o dashboard
"""
 
import os
import json
import hmac
import hashlib
import base64
import time
import urllib.parse
import logging
from datetime import datetime, timezone
from typing import Optional
from contextlib import asynccontextmanager
 
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import httpx
import uvicorn
from signals import run_signal_loop
 
# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("tradebot")
 
# ── Config ─────────────────────────────────────────────────────────────────
KRAKEN_API_KEY     = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET  = os.getenv("KRAKEN_API_SECRET", "")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "muda_este_secret")
PAPER_TRADING      = os.getenv("PAPER_TRADING", "true").lower() == "true"
MAX_POSITION_USDT  = float(os.getenv("MAX_POSITION_USDT", "50"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "5"))
 
KRAKEN_BASE = "https://api.kraken.com"
 
# ── Estado em memória ──────────────────────────────────────────────────────
state = {
    "signals": [],
    "trades": [],
    "positions": {},
    "pnl_daily": [],
    "balance_usdt": 0.0,
    "total_pnl": 0.0,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "errors": [],
}
 
# ── Kraken helpers ─────────────────────────────────────────────────────────
def _kraken_sign(urlpath: str, data: dict, secret: str) -> str:
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()
 
def _kraken_headers(urlpath: str, data: dict) -> dict:
    return {
        "API-Key": KRAKEN_API_KEY,
        "API-Sign": _kraken_sign(urlpath, data, KRAKEN_API_SECRET),
        "Content-Type": "application/x-www-form-urlencoded",
    }
 
async def kraken_public(endpoint: str, params: dict = None) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{KRAKEN_BASE}/0/public/{endpoint}", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise Exception(f"Kraken API error: {data['error']}")
        return data["result"]
 
async def kraken_private(endpoint: str, data: dict = None) -> dict:
    if data is None:
        data = {}
    data["nonce"] = str(int(time.time() * 1000))
    urlpath = f"/0/private/{endpoint}"
    headers = _kraken_headers(urlpath, data)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{KRAKEN_BASE}{urlpath}",
            data=data,
            headers=headers,
            timeout=10
        )
        r.raise_for_status()
        result = r.json()
        if result.get("error"):
            raise Exception(f"Kraken API error: {result['error']}")
        return result["result"]
 
# ── Converter símbolo para par Kraken ──────────────────────────────────────
def to_kraken_pair(symbol: str) -> str:
    # BTC/USDT → XBTUSD, ETH/USDT → ETHUSD, etc.
    symbol = symbol.upper().replace("/", "").replace("USDT", "USD")
    mapping = {
        "BTCUSD": "XBTUSD",
        "ETHUSD": "ETHUSD",
        "SOLUSD": "SOLUSD",
        "XRPUSD": "XRPUSD",
        "ADAUSD": "ADAUSD",
        "DOTUSD": "DOTUSD",
    }
    return mapping.get(symbol, symbol)
 
# ── Lógica de risco ────────────────────────────────────────────────────────
def check_risk(symbol: str, side: str, usdt_size: float) -> tuple[bool, str]:
    if usdt_size > MAX_POSITION_USDT:
        return False, f"Tamanho {usdt_size} USDT excede limite {MAX_POSITION_USDT} USDT"
 
    daily_loss = sum(
        t["pnl"] for t in state["trades"]
        if t["pnl"] < 0 and t["date"] == datetime.now().strftime("%Y-%m-%d")
    )
    if state["balance_usdt"] > 0:
        loss_pct = abs(daily_loss) / state["balance_usdt"] * 100
        if loss_pct >= MAX_DAILY_LOSS_PCT:
            return False, f"Limite de perda diária atingido ({loss_pct:.1f}%)"
 
    if symbol in state["positions"] and state["positions"][symbol]["side"] == side:
        return False, f"Já existe posição {side} aberta em {symbol}"
 
    return True, "ok"
 
# ── Executar ordem ─────────────────────────────────────────────────────────
async def execute_order(signal: dict):
    symbol    = signal["symbol"].upper()
    action    = signal["action"].upper()
    size_usd  = float(signal.get("size_usd", MAX_POSITION_USDT))
    kraken_pair = to_kraken_pair(symbol)
 
    log.info(f"Sinal recebido: {action} {symbol} (${size_usd})")
 
    if PAPER_TRADING:
        trade = {
            "id": f"paper_{int(time.time())}",
            "symbol": symbol,
            "side": action,
            "size_usd": size_usd,
            "price": float(signal.get("price", 0)),
            "status": "FILLED (paper)",
            "pnl": 0.0,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        state["trades"].insert(0, trade)
        state["trades"] = state["trades"][:100]
 
        if action in ("BUY", "SELL"):
            state["positions"][symbol] = {
                "symbol": symbol,
                "side": action,
                "size_usd": size_usd,
                "entry_price": float(signal.get("price", 0)),
                "current_price": float(signal.get("price", 0)),
                "pnl": 0.0,
                "opened_at": datetime.now(timezone.utc).isoformat(),
            }
        elif action == "CLOSE" and symbol in state["positions"]:
            del state["positions"][symbol]
 
        log.info(f"Paper trade executado: {trade}")
        return trade
 
    # Real trading
    ok, reason = check_risk(symbol, action, size_usd)
    if not ok:
        log.warning(f"Ordem bloqueada por risco: {reason}")
        state["errors"].insert(0, {"msg": reason, "ts": datetime.now(timezone.utc).isoformat()})
        state["errors"] = state["errors"][:20]
        return None
 
    try:
        # Obtém preço actual
        ticker = await kraken_public("Ticker", {"pair": kraken_pair})
        price = float(list(ticker.values())[0]["c"][0])
        volume = round(size_usd / price, 6)
 
        kraken_side = "buy" if action == "BUY" else "sell"
        result = await kraken_private("AddOrder", {
            "pair": kraken_pair,
            "type": kraken_side,
            "ordertype": "market",
            "volume": str(volume),
        })
        log.info(f"Ordem executada na Kraken: {result}")
        return result
 
    except Exception as e:
        err = f"Erro ao executar ordem {symbol}: {e}"
        log.error(err)
        state["errors"].insert(0, {"msg": err, "ts": datetime.now(timezone.utc).isoformat()})
        return None
 
# ── Actualizar posições ─────────────────────────────────────────────────────
async def refresh_positions():
    if not state["positions"] or PAPER_TRADING:
        return
    try:
        for symbol, pos in state["positions"].items():
            kraken_pair = to_kraken_pair(symbol)
            ticker = await kraken_public("Ticker", {"pair": kraken_pair})
            pos["current_price"] = float(list(ticker.values())[0]["c"][0])
            if pos["entry_price"] > 0:
                pct = (pos["current_price"] - pos["entry_price"]) / pos["entry_price"]
                if pos["side"] == "SELL":
                    pct = -pct
                pos["pnl"] = round(pos["size_usd"] * pct, 2)
    except Exception as e:
        log.error(f"Erro ao actualizar posições: {e}")
 
# ── FastAPI app ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"TradeBot iniciado | Paper trading: {PAPER_TRADING} | Exchange: Kraken")
    asyncio.create_task(run_signal_loop(state))
    yield
 
app = FastAPI(title="TradeBot", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")
 
# ── Webhook do TradingView ──────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON inválido")
 
    if body.get("secret") != WEBHOOK_SECRET:
        log.warning(f"Webhook com secret inválido de {request.client.host}")
        raise HTTPException(403, "Secret inválido")
 
    signal = {
        "symbol": body.get("symbol", "???"),
        "action": body.get("action", "???"),
        "price":  body.get("price", 0),
        "size_usd": body.get("size_usd", MAX_POSITION_USDT),
        "strategy": body.get("strategy", "manual"),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    state["signals"].insert(0, signal)
    state["signals"] = state["signals"][:50]
 
    log.info(f"Webhook recebido: {signal}")
    background_tasks.add_task(execute_order, signal)
 
    return {"status": "recebido", "signal": signal}
 
# ── API REST para o dashboard ───────────────────────────────────────────────
@app.get("/api/status")
async def api_status():
    await refresh_positions()
    return {
        "paper_trading": PAPER_TRADING,
        "exchange": "Kraken",
        "started_at": state["started_at"],
        "balance_usdt": state["balance_usdt"],
        "total_pnl": state["total_pnl"],
        "open_positions": len(state["positions"]),
        "total_trades": len(state["trades"]),
        "errors": state["errors"][:5],
    }
 
@app.get("/api/positions")
async def api_positions():
    await refresh_positions()
    return list(state["positions"].values())
 
@app.get("/api/signals")
async def api_signals():
    return state["signals"]
 
@app.get("/api/trades")
async def api_trades():
    return state["trades"][:50]
 
@app.get("/api/health")
async def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}
 
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("static/index.html") as f:
        return f.read()
 
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
