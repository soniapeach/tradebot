"""
TradeBot - Servidor principal
Recebe webhooks do TradingView, executa ordens na Binance, serve o dashboard
"""

import os
import json
import hmac
import hashlib
import time
import logging
from datetime import datetime, timezone
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import uvicorn

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("tradebot")

# ── Config ─────────────────────────────────────────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "muda_este_secret")
PAPER_TRADING      = os.getenv("PAPER_TRADING", "true").lower() == "true"
MAX_POSITION_USDT  = float(os.getenv("MAX_POSITION_USDT", "50"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "5"))

BINANCE_BASE = "https://testnet.binancefutures.com" if PAPER_TRADING else "https://fapi.binance.com"

# ── Estado em memória (substituir por SQLite em produção) ──────────────────
state = {
    "signals": [],       # últimos 50 sinais recebidos
    "trades": [],        # histórico de trades executados
    "positions": {},     # posições abertas { symbol: {...} }
    "pnl_daily": [],     # P&L por dia (últimos 30 dias)
    "balance_usdt": 0.0,
    "total_pnl": 0.0,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "errors": [],
}

# ── Binance helpers ────────────────────────────────────────────────────────
def _sign(params: dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(
        BINANCE_API_SECRET.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()

async def binance_get(path: str, params: dict = None) -> dict:
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BINANCE_BASE}{path}", params=params, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()

async def binance_post(path: str, params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{BINANCE_BASE}{path}", params=params, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()

# ── Lógica de risco ────────────────────────────────────────────────────────
def check_risk(symbol: str, side: str, usdt_size: float) -> tuple[bool, str]:
    """Verifica regras de risco antes de executar ordem"""
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
    symbol   = signal["symbol"].upper().replace("/", "")  # BTC/USDT → BTCUSDT
    action   = signal["action"].upper()   # BUY / SELL / CLOSE
    size_usd = float(signal.get("size_usd", MAX_POSITION_USDT))

    log.info(f"Sinal recebido: {action} {symbol} ${size_usd}")

    if PAPER_TRADING:
        # Simula execução em paper trading
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

    # Real trading - verifica risco primeiro
    ok, reason = check_risk(symbol, action, size_usd)
    if not ok:
        log.warning(f"Ordem bloqueada por risco: {reason}")
        state["errors"].insert(0, {"msg": reason, "ts": datetime.now(timezone.utc).isoformat()})
        state["errors"] = state["errors"][:20]
        return None

    try:
        # Obtém preço actual e calcula quantidade
        ticker = await binance_get("/fapi/v1/ticker/price", {"symbol": symbol})
        price = float(ticker["price"])
        quantity = round(size_usd / price, 3)

        result = await binance_post("/fapi/v1/order", {
            "symbol": symbol,
            "side": action,
            "type": "MARKET",
            "quantity": quantity,
        })
        log.info(f"Ordem executada na Binance: {result}")
        return result

    except Exception as e:
        err = f"Erro ao executar ordem {symbol}: {e}"
        log.error(err)
        state["errors"].insert(0, {"msg": err, "ts": datetime.now(timezone.utc).isoformat()})
        return None

# ── Actualizar posições ─────────────────────────────────────────────────────
async def refresh_positions():
    """Actualiza preços e P&L das posições abertas"""
    if not state["positions"] or PAPER_TRADING:
        return
    try:
        for symbol, pos in state["positions"].items():
            ticker = await binance_get("/fapi/v1/ticker/price", {"symbol": symbol})
            pos["current_price"] = float(ticker["price"])
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
    log.info(f"TradeBot iniciado | Paper trading: {PAPER_TRADING}")
    yield

app = FastAPI(title="TradeBot", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Serve o dashboard (ficheiros estáticos)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Webhook do TradingView ──────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Endpoint que recebe alertas do TradingView.
    
    Formato esperado do alerta (JSON):
    {
        "secret": "o_teu_webhook_secret",
        "symbol": "BTC/USDT",
        "action": "BUY",      // BUY, SELL, ou CLOSE
        "price": 64500,
        "size_usd": 50,
        "strategy": "EMA Cross"
    }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON inválido")

    # Valida secret
    if body.get("secret") != WEBHOOK_SECRET:
        log.warning(f"Webhook com secret inválido de {request.client.host}")
        raise HTTPException(403, "Secret inválido")

    # Regista sinal
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

    # Executa ordem em background (não bloqueia resposta)
    background_tasks.add_task(execute_order, signal)

    return {"status": "recebido", "signal": signal}

# ── API REST para o dashboard ───────────────────────────────────────────────
@app.get("/api/status")
async def api_status():
    await refresh_positions()
    return {
        "paper_trading": PAPER_TRADING,
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

# ── Dashboard (redireciona para static/index.html) ─────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("static/index.html") as f:
        return f.read()

# ── Entrypoint ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
