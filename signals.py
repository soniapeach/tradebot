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
    """
    Busca velas horárias da Bitget para um símbolo.
    Retorna lista de dicts com keys: open, high, low, close, volume
    """
    url = f"{BITGET_BASE}/api/v2/mix/market/candles"
    params = {
        "symbol": symbol,
        "productType": "usdt-futures",
        "granularity": "1H",   # 60 minutos
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
            # Formato: [timestamp, open, high, low, close, volume, ...]
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
 
 
# ── Indicadores técnicos (Python puro, sem dependências) ───────────────────
def ema(values: list[float], period: int) -> list[float]:
    """Exponential Moving Average"""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result
 
 
def rsi(values: list[float], period: int = 14) -> float:
    """Relative Strength Index (último valor)"""
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
    """MACD — retorna (macd_line, signal_line, histogram) do último valor"""
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
 
 
# ── Gerador de sinais ──────────────────────────────────────────────────────
def generate_signal(symbol: str, candles: list[dict]) -> dict | None:
    """
    Estratégia EMA 9/21 Cross com filtro RSI.
    Retorna dict com sinal ou None se não houver sinal.
    """
    if len(candles) < 30:
        log.warning(f"{symbol}: velas insuficientes ({len(candles)})")
        return None
 
    closes = [c["close"] for c in candles]
 
    ema9  = ema(closes, 9)
    ema21 = ema(closes, 21)
    rsi_val = rsi(closes, 14)
    macd_line, macd_signal, macd_hist = macd(closes)
 
    if len(ema9) < 2 or len(ema21) < 2:
        return None
 
    # Cruzamento actual e anterior
    ema9_now,  ema21_now  = ema9[-1],  ema21[-1]
    ema9_prev, ema21_prev = ema9[-2],  ema21[-2]
    price = closes[-1]
 
    action = None
 
    # BUY: EMA9 cruza acima EMA21 + RSI não sobrecomprado
    if ema9_prev <= ema21_prev and ema9_now > ema21_now:
        if rsi_val < 65:
            action = "BUY"
        else:
            log.info(f"{symbol}: cruzamento BUY filtrado — RSI {rsi_val} > 65")
 
    # SELL: EMA9 cruza abaixo EMA21 + RSI não sobrevendido
    elif ema9_prev >= ema21_prev and ema9_now < ema21_now:
        if rsi_val > 35:
            action = "SELL"
        else:
            log.info(f"{symbol}: cruzamento SELL filtrado — RSI {rsi_val} < 35")
 
    if not action:
        log.info(f"{symbol}: sem sinal | preço={price} EMA9={ema9_now:.2f} EMA21={ema21_now:.2f} RSI={rsi_val}")
        return None
 
    signal = {
        "symbol":    symbol.replace("USDT", "/USDT"),  # BTCUSDT → BTC/USDT
        "action":    action,
        "price":     price,
        "rsi":       rsi_val,
        "ema9":      round(ema9_now, 2),
        "ema21":     round(ema21_now, 2),
        "macd":      macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "strategy":  "EMA 9/21 Cross + RSI Filter",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    log.info(f"✅ Sinal gerado: {action} {symbol} @ {price} | RSI={rsi_val}")
    return signal
 
 
# ── Loop principal ─────────────────────────────────────────────────────────
async def run_signal_loop(state: dict):
    """
    Loop que corre em background.
    Recebe o dict `state` do main.py para adicionar sinais directamente.
    """
    log.info(f"Motor de sinais iniciado — intervalo: {SIGNAL_INTERVAL_MINUTES} min")
 
    while True:
        log.info("🔍 A analisar mercados...")
        for symbol in SYMBOLS:
            try:
                candles = await fetch_candles(symbol, limit=100)
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
