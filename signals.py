"""
TradeBot - Motor de Sinais Automático
Corre em background de hora a hora.
Busca velas à Bitget, calcula EMA/RSI/MACD, gera sinais BUY/SELL.
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

# ── Bitget public API — busca velas (sem autenticação) ─────────────────────
async def fetch_candles(symbol: str, limit: int = 100) -> list[dict]:
    url = f"{BITGET_BASE}/api/v2/mix/market/candles"
    params = {
        "symbol": symbol + "_UMCBL",
        "productType": "umcbl",
        "granularity": "60",
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
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    if len(values) < slow + signal:
        return 0.0, 0.0, 0.0
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    min_len = min(len(ema_fast), len(ema_slow))
    macd_line = [ema_fast[-(min_len - i)] - ema_slow[-(min_len - i)] for i in range(min_len)]
    signal_line = ema(macd_line, signal)
    if not signal_line:
        return 0.0, 0.0, 0.0
    m = macd_line[-1]
    s = signal_line[-1]
    return round(m, 4), round(s, 4), round(m - s, 4)


def generate_signal(symbol: str, candles: list[dict]) -> dict | None:
    if
