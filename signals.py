"""
TradeBot - Motor de Sinais Automático
Estratégia: EMA 20/50 Cross + RSI + ATR Stop/Target
Velas de 4 horas da Bitget.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

log = logging.getLogger("tradebot.signals")

BITGET_BASE = "https://api.bitget.com"
SIGNAL_INTERVAL_MINUTES = int(os.getenv("SIGNAL_INTERVAL_MINUTES", "60"))
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# ── Parâmetros da estratégia ───────────────────────────────────────────────
EMA_FAST     = 20
EMA_SLOW     = 50
RSI_LEN      = 14
RSI_LO_BUY  = 45   # RSI mínimo para Long
RSI_HI_BUY  = 70   # RSI máximo para Long
RSI_LO_SELL = 30   # RSI mínimo para Short
RSI_HI_SELL = 55   # RSI máximo para Short
ATR_LEN      = 14
SL_MULT      = 1.5  # Stop Loss = 1.5 × ATR
TP_MULT      = 2.5  # Take Profit = 2.5 × ATR

# ── Bitget public API — busca velas de 4H ─────────────────────────────────
async def fetch_candles(symbol: str, limit: int = 100) -> list[dict]:
    url = f"{BITGET_BASE}/api/v2/mix/market/candles"
    params = {
        "symbol": symbol,
        "productType": "usdt-futures",
        "granularity": "4H",
        "limit": str(limit),
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != "00000":
                log.error(f"Bitget candles erro {symbol}: {data}")
                return []
            candles = []
            for c in data["data"]:
                candles.append({
                    "open":   float(c[1]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "close":  float(c[4]),
                    "volume": float(c[5]),
                })
            return candles
    except Exception as e:
        log.error(f"Erro ao buscar velas {symbol}: {e}")
        return []

# ── Indicadores técnicos ───────────────────────────────────────────────────
def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains  = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)

def atr(candles: list[dict], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return round(sum(trs[-period:]) / period, 4)

# ── Gerador de sinais ──────────────────────────────────────────────────────
def generate_signal(symbol: str, candles: list[dict]) -> dict | None:
    if len(candles) < EMA_SLOW + 2:
        log.warning(f"{symbol}: velas insuficientes ({len(candles)})")
        return None

    closes = [c["close"] for c in candles]

    ema_f = ema(closes, EMA_FAST)
    ema_s = ema(closes, EMA_SLOW)
    rsi_val = rsi(closes, RSI_LEN)
    atr_val = atr(candles, ATR_LEN)
    price = closes[-1]

    if len(ema_f) < 2 or len(ema_s) < 2:
        return None

    ema_f_now,  ema_s_now  = ema_f[-1],  ema_s[-1]
    ema_f_prev, ema_s_prev = ema_f[-2],  ema_s[-2]

    cross_up   = ema_f_prev <= ema_s_prev and ema_f_now > ema_s_now
    cross_down = ema_f_prev >= ema_s_prev and ema_f_now < ema_s_now

    action = None
    sl_price = None
    tp_price = None

    if cross_up and RSI_LO_BUY <= rsi_val <= RSI_HI_BUY:
        action   = "BUY"
        sl_price = round(price - SL_MULT * atr_val, 2)
        tp_price = round(price + TP_MULT * atr_val, 2)

    elif cross_down and RSI_LO_SELL <= rsi_val <= RSI_HI_SELL:
        action   = "SELL"
        sl_price = round(price + SL_MULT * atr_val, 2)
        tp_price = round(price - TP_MULT * atr_val, 2)

    if not action:
        log.info(f"{symbol}: sem sinal | preço={price:.2f} EMA{EMA_FAST}={ema_f_now:.2f} EMA{EMA_SLOW}={ema_s_now:.2f} RSI={rsi_val}")
        return None

    signal = {
        "symbol":       symbol.replace("USDT", "/USDT"),
        "action":       action,
        "price":        price,
        "sl_price":     sl_price,
        "tp_price":     tp_price,
        "rsi":          rsi_val,
        "atr":          atr_val,
        f"ema{EMA_FAST}": round(ema_f_now, 2),
        f"ema{EMA_SLOW}": round(ema_s_now, 2),
        "strategy":     f"EMA {EMA_FAST}/{EMA_SLOW} + RSI + ATR 4H",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    log.info(f"✅ Sinal: {action} {symbol} @ {price} | SL={sl_price} TP={tp_price} | RSI={rsi_val} ATR={atr_val}")
    return signal

# ── Loop principal ─────────────────────────────────────────────────────────
async def run_signal_loop(state: dict):
    log.info(f"Motor de sinais iniciado — intervalo: {SIGNAL_INTERVAL_MINUTES} min | Estratégia: EMA {EMA_FAST}/{EMA_SLOW} + RSI + ATR 4H")
    while True:
        log.info("🔍 A analisar mercados (4H)...")
        for symbol in SYMBOLS:
            try:
                candles = await fetch_candles(symbol, limit=120)
                if not candles:
                    continue
                signal = generate_signal(symbol, candles)
                if signal:
                    state["signals"].insert(0, signal)
                    state["signals"] = state["signals"][:50]
            except Exception as e:
                log.error(f"Erro no loop de sinais {symbol}: {e}")
        log.info(f"✅ Análise concluída. Próxima em {SIGNAL_INTERVAL_MINUTES} min.")
        await asyncio.sleep(SIGNAL_INTERVAL_MINUTES * 60)
