"""
indicators.py — Cálculo de indicadores técnicos, fundamentales, y scoring.

Incluye: fetch_ticker, fetch_intraday, get_fundamentals, ATR,
         caches con thread-safety, conversión EUR/USD.
"""
import logging
import threading
import time
import math
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("tracker")

import yfinance as yf

from config import (
    GROUPS, USD_TICKERS, INTRADAY_TTL, FUND_TTL, FUND_MAX,
    CHART_TTL, CHART_MAX,
    ATR_TRAILING_MULT, ATR_TRAILING_FLOOR, ATR_TRAILING_CAP, ATR_DEFAULT_PCT,
)

# ── EUR/USD ───────────────────────────────────────────────────────────────────
# IMPORTANTE: _eurusd_rate almacena el tipo USD→EUR (cuántos EUR vale 1 USD).
# Es el INVERSO del tipo de mercado estándar EUR/USD (~1.10).
# Ejemplo: si EUR/USD = 1.10 → _eurusd_rate = 1/1.10 ≈ 0.909
# Uso: precio_EUR = precio_USD * _eurusd_rate
#      precio_USD = precio_EUR / _eurusd_rate
# En app.py: set_eurusd(round(1 / _rate, 6))  ← correcto, _rate es el tipo de mercado
_eurusd_rate: float = 1.0
_eurusd_lock = threading.Lock()

def get_eurusd():
    with _eurusd_lock:
        return _eurusd_rate

def set_eurusd(rate: float):
    """Recibe el tipo USD→EUR (inverso del mercado). Si rate > 1.5 probablemente
    se está pasando el tipo de mercado sin invertir — se corrige automáticamente."""
    global _eurusd_rate
    with _eurusd_lock:
        if rate > 1.5:
            # Valor anómalo: casi con certeza es el tipo EUR/USD sin invertir
            log.warning("set_eurusd recibió %.4f (>1.5) — se interpreta como tipo de mercado y se invierte", rate)
            rate = round(1.0 / rate, 6)
        _eurusd_rate = rate

# ── FUNDAMENTALS CACHE (thread-safe) ─────────────────────────────────────────
_fund_cache = {}
_fund_lock  = threading.Lock()

def _fund_prune():
    if len(_fund_cache) <= FUND_MAX:
        return
    sorted_keys = sorted(_fund_cache, key=lambda k: _fund_cache[k]["ts"])
    for k in sorted_keys[:len(sorted_keys) // 5]:
        _fund_cache.pop(k, None)

def get_fundamentals(ticker):
    now = time.time()
    with _fund_lock:
        c = _fund_cache.get(ticker)
        if c and (now - c["ts"]) < FUND_TTL:
            return c["pe"], c["div"], c.get("beta")

    pe_ratio = div_yield = beta = None
    full = None
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info
        pe_raw = getattr(info, "pe_ratio", None)
        if pe_raw and pe_raw > 0:
            pe_ratio = round(float(pe_raw), 1)
        dy_raw = getattr(info, "dividend_yield", None)
        if dy_raw is None:
            full = t.info
            if pe_ratio is None:
                pe_v = full.get("trailingPE") or 0
                if pe_v > 0: pe_ratio = round(float(pe_v), 1)
            dy2 = full.get("dividendYield") or full.get("yield")
            if dy2 and dy2 > 0: div_yield = round(float(dy2) * 100, 2)
        elif dy_raw > 0:
            div_yield = round(float(dy_raw) * 100, 2)
    except Exception:
        pass

    try:
        if full is None:
            full = t.info  # reutilizar el mismo Ticker, no crear uno nuevo
        beta_raw = full.get("beta")
        if beta_raw is not None and math.isfinite(float(beta_raw)):
            beta = round(float(beta_raw), 2)
    except Exception:
        pass

    with _fund_lock:
        _fund_cache[ticker] = {"pe": pe_ratio, "div": div_yield, "beta": beta, "ts": now}
        _fund_prune()
    return pe_ratio, div_yield, beta

# ── 5Y HIST CACHE (thread-safe) ──────────────────────────────────────────────
# El histórico 5y solo se usa para ret_1y, ret_3y y prob_up_30d — cambia muy poco.
# TTL de 24h: reduce de 107 a 0 peticiones extra por ciclo una vez calentado.
_hist5y_cache: dict = {}
_hist5y_lock  = threading.Lock()
_HIST5Y_TTL   = 86400  # 24 horas

def _get_hist5y(ticker, t):
    """Devuelve el histórico 5y (solo Close) desde caché o yfinance."""
    now = time.monotonic()
    with _hist5y_lock:
        hit = _hist5y_cache.get(ticker)
        if hit and (now - hit["ts"]) < _HIST5Y_TTL:
            return hit["data"]
    try:
        raw = t.history(period="5y", auto_adjust=True)
        if not raw.empty:
            raw.index = raw.index.tz_localize(None) if raw.index.tzinfo else raw.index
            data = raw[["Close"]].copy()
        else:
            data = None
    except Exception:
        data = None
    with _hist5y_lock:
        # Evicción LRU: máximo 120 tickers en caché (83 tickers + margen)
        if len(_hist5y_cache) >= 120:
            oldest = min(_hist5y_cache, key=lambda k: _hist5y_cache[k]["ts"])
            _hist5y_cache.pop(oldest, None)
        _hist5y_cache[ticker] = {"ts": now, "data": data}
    return data

# ── INTRADAY CACHE (thread-safe) ─────────────────────────────────────────────
_intraday_cache: dict = {}
_intraday_lock = threading.Lock()

def pct(new, old):
    if old and old != 0 and new:
        return round((new - old) / abs(old) * 100, 1)
    return None

def fetch_intraday(ticker):
    """Returns (r15, r60, r180, up_vol, dn_vol, last_price)."""
    _now = time.monotonic()
    with _intraday_lock:
        hit = _intraday_cache.get(ticker)
        if hit and (_now - hit[0]) < INTRADAY_TTL:
            return hit[1]
    try:
        t = yf.Ticker(ticker)
        intra = t.history(period="5d", interval="1h", auto_adjust=True)
        if intra.empty or len(intra) < 2:
            return None, None, None, None, None, None
        intra.index = intra.index.tz_localize(None) if intra.index.tzinfo else intra.index

        now_price = float(intra["Close"].iloc[-1])
        now_time  = intra.index[-1]

        hourly_rets = intra["Close"].pct_change().dropna()
        hourly_vol  = float(hourly_rets.std()) if len(hourly_rets) >= 5 else None

        if len(intra) >= 4:
            p3h      = float(intra["Close"].iloc[-4])
            momentum = (now_price - p3h) / p3h
        else:
            momentum = 0.0

        if hourly_vol:
            bias     = momentum * 0.3
            up_range = round((hourly_vol + max(bias, 0)) * 100, 2)
            dn_range = round((hourly_vol - min(bias, 0)) * 100, 2)
        else:
            up_range = dn_range = None

        def price_h_ago(hours):
            target = now_time - timedelta(hours=hours)
            subset = intra[intra.index <= target]
            return float(subset["Close"].iloc[-1]) if not subset.empty else None

        r15  = pct(now_price, price_h_ago(0.25))
        r60  = pct(now_price, price_h_ago(1))
        r180 = pct(now_price, price_h_ago(3))

        result = (r15, r60, r180, up_range, dn_range, now_price)
        with _intraday_lock:
            _intraday_cache[ticker] = (time.monotonic(), result)
        return result
    except Exception:
        return None, None, None, None, None, None

def purge_intraday_cache():
    """Purga entradas expiradas del intraday cache. Límite máximo 120 entradas."""
    _now = time.monotonic()
    with _intraday_lock:
        expired = [k for k, v in _intraday_cache.items() if (_now - v[0]) > INTRADAY_TTL * 3]
        for k in expired:
            _intraday_cache.pop(k, None)
        # Evicción por tamaño: si supera 120 entradas, eliminar las más antiguas
        if len(_intraday_cache) > 120:
            overflow = sorted(_intraday_cache, key=lambda k: _intraday_cache[k][0])
            for k in overflow[:len(_intraday_cache) - 100]:
                _intraday_cache.pop(k, None)
    if expired:
        log.debug("purgadas %d entradas intraday", len(expired))

# ── ATR CALCULATION ───────────────────────────────────────────────────────────
_atr_cache: dict = {}
_atr_lock = threading.Lock()
_ATR_TTL = 3600  # 1 hora — ATR no cambia rápido

def calc_atr_pct(ticker, hist=None):
    """Calcula el ATR(14) como % del precio. Cached 1h.
    Acepta hist pre-descargado (de fetch_ticker) para evitar un request extra a yfinance.
    Devuelve float (ej: 2.5 para 2.5%) o None."""
    now = time.monotonic()
    with _atr_lock:
        hit = _atr_cache.get(ticker)
        if hit and (now - hit[0]) < _ATR_TTL:
            return hit[1]
    try:
        if hist is None or hist.empty or len(hist) < 15:
            t = yf.Ticker(ticker)
            hist = t.history(period="1mo", auto_adjust=True)
        if hist.empty or len(hist) < 15:
            return None
        high = hist["High"]
        low  = hist["Low"]
        close = hist["Close"]
        # True Range (vectorizado — evita el bucle iloc que era incorrecto y lento)
        import pandas as pd
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low  - close.shift(1)).abs()
        tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        price = float(close.iloc[-1])
        if price > 0 and math.isfinite(atr):
            atr_pct = round(atr / price * 100, 2)
            with _atr_lock:
                # Evicción por tamaño: máximo 150 entradas
                if len(_atr_cache) >= 150:
                    oldest = min(_atr_cache, key=lambda k: _atr_cache[k][0])
                    _atr_cache.pop(oldest, None)
                _atr_cache[ticker] = (time.monotonic(), atr_pct)
            return atr_pct
    except Exception:
        pass
    return None

def get_trailing_pct(ticker, hist=None):
    """Devuelve el trailing stop % adaptado a la volatilidad del ticker.
    SHY (~0.3% ATR) → 1.5% trailing
    SPY (~1.2% ATR) → 1.8% trailing
    ARKK (~3.5% ATR) → 5.0% trailing (capped)
    """
    atr_pct = calc_atr_pct(ticker, hist=hist)
    if atr_pct is None:
        return ATR_DEFAULT_PCT
    trailing = atr_pct * ATR_TRAILING_MULT
    return round(max(ATR_TRAILING_FLOOR, min(ATR_TRAILING_CAP, trailing)), 2)

# ── SPY + VIX BENCHMARK — cache unificado ────────────────────────────────────
# Cubre: get_spy_returns, get_spy_regime, get_market_regime (scoring adaptativo)
_spy_unified_cache = {"hist": None, "returns": None, "regime": None, "scoring_regime": None, "ts": 0}
_spy_unified_lock  = threading.Lock()
_SPY_TTL    = 600   # 10 min — usado por get_spy_returns
_REGIME_TTL = 900   # 15 min — usado por get_spy_regime y get_market_regime


def _refresh_spy_cache():
    """Descarga SPY (6mo), URTH (6mo) y VIX (1mo) en un solo ciclo.
    Rellena returns, regime (momentum/dip_buying) y scoring_regime (4 niveles).
    """
    result = {}
    spy_close = None

    for sym in ("SPY", "URTH"):
        try:
            t = yf.Ticker(sym)
            hist = t.history(period="6mo", auto_adjust=True)
            if hist.empty or len(hist) < 50:
                result[sym] = {"price": None, "sma50": None, "above": None}
                continue
            hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
            close = hist["Close"]
            price = float(close.iloc[-1])
            sma50 = float(close.rolling(50).mean().iloc[-1])
            result[sym] = {"price": round(price, 2), "sma50": round(sma50, 2), "above": price > sma50}
            if sym == "SPY":
                spy_close = close
        except Exception as e:
            log.warning("spy error %s: %s", sym, e)
            result[sym] = {"price": None, "sma50": None, "above": None}

    # ── VIX — media 5 dias para evitar cambios de regimen por spike puntual ──
    vix_current = None
    vix_sma5    = None
    try:
        vix_hist = yf.Ticker("^VIX").history(period="1mo", auto_adjust=True)
        if not vix_hist.empty and len(vix_hist) >= 5:
            vix_hist.index = vix_hist.index.tz_localize(None) if vix_hist.index.tzinfo else vix_hist.index
            vix_close   = vix_hist["Close"]
            vix_current = round(float(vix_close.iloc[-1]), 2)
            vix_sma5    = round(float(vix_close.rolling(5).mean().iloc[-1]), 2)
    except Exception as e:
        log.warning("vix error: %s", e)

    # ── Returns de SPY ────────────────────────────────────────────────────────
    spy_returns = None
    if spy_close is not None and len(spy_close) >= 22:
        price = float(spy_close.iloc[-1])
        def _ret(days):
            if len(spy_close) > days:
                old = float(spy_close.iloc[-days - 1])
                return (price - old) / old * 100 if old > 0 else None
            return None
        spy_returns = {"ret_5d": _ret(5), "ret_14d": _ret(14), "ret_21d": _ret(21), "price": price}

    # ── Regimen momentum/dip_buying (usado por paper_engine) ─────────────────
    spy_above  = result.get("SPY",  {}).get("above")
    urth_above = result.get("URTH", {}).get("above")
    if spy_above is True and urth_above is True:
        regime = "momentum"
        note = (f"SPY {result['SPY']['price']} > SMA50 {result['SPY']['sma50']} ✓  |  "
                f"URTH {result['URTH']['price']} > SMA50 {result['URTH']['sma50']} ✓")
    elif spy_above is None or urth_above is None:
        regime = "dip_buying"
        note = "Sin datos suficientes — modo dip_buying por defecto"
    else:
        regime = "dip_buying"
        spy_str  = f"SPY {result['SPY']['price']} {'>' if spy_above else '<'} SMA50 {result['SPY']['sma50']}"
        urth_str = f"URTH {result['URTH']['price']} {'>' if urth_above else '<'} SMA50 {result['URTH']['sma50']}"
        note = f"{spy_str}  |  {urth_str}"

    regime_data = {
        "regime": regime,
        "spy_price":        result.get("SPY",  {}).get("price"),
        "spy_sma50":        result.get("SPY",  {}).get("sma50"),
        "spy_above_sma50":  spy_above,
        "urth_price":       result.get("URTH", {}).get("price"),
        "urth_sma50":       result.get("URTH", {}).get("sma50"),
        "urth_above_sma50": urth_above,
        "vix":              vix_current,
        "vix_sma5":         vix_sma5,
        "note": note,
    }

    # ── Scoring regime — 4 niveles para pesos adaptativos del inv_score ───────
    # Usa VIX suavizado (sma5) para evitar oscilaciones por spike de 1 dia
    vix_ref = vix_sma5 if vix_sma5 is not None else vix_current
    if vix_ref is not None and vix_ref > 30:
        scoring_regime = "BEAR_PANIC"
    elif spy_above is False and vix_ref is not None and vix_ref > 18:
        scoring_regime = "BEAR_MODERATE"
    elif spy_above is True and (vix_ref is None or vix_ref < 18):
        scoring_regime = "BULL_QUIET"
    else:
        scoring_regime = "BULL_VOLATILE"  # fallback: pesos estandar

    with _spy_unified_lock:
        _spy_unified_cache["returns"]        = spy_returns
        _spy_unified_cache["regime"]         = regime_data
        _spy_unified_cache["scoring_regime"] = scoring_regime
        _spy_unified_cache["ts"]             = time.monotonic()


def get_spy_returns():
    """Retornos de SPY a distintos plazos. Cached _SPY_TTL."""
    now = time.monotonic()
    with _spy_unified_lock:
        if _spy_unified_cache["returns"] is not None and (now - _spy_unified_cache["ts"]) < _SPY_TTL:
            return _spy_unified_cache["returns"]
    _refresh_spy_cache()
    with _spy_unified_lock:
        return _spy_unified_cache["returns"]


def get_spy_regime():
    """
    Detecta el regimen de mercado usando SPY y URTH vs su SMA50.
    Devuelve un dict con regime, spy_price, spy_sma50, urth_price, urth_sma50, vix, note.
    Cache compartido con get_spy_returns — un solo fetch cubre ambas funciones.
    """
    now = time.monotonic()
    with _spy_unified_lock:
        if _spy_unified_cache["regime"] is not None and (now - _spy_unified_cache["ts"]) < _REGIME_TTL:
            return _spy_unified_cache["regime"]
        # Si los datos llevan >30min sin actualizarse (fallo red), forzar reset en próximo intento
        if _spy_unified_cache["ts"] > 0 and (now - _spy_unified_cache["ts"]) > _REGIME_TTL * 2:
            log.warning("[spy_cache] datos stale >30min — forzando recarga")
            _spy_unified_cache["ts"] = 0
    _refresh_spy_cache()
    with _spy_unified_lock:
        return _spy_unified_cache["regime"] or {"regime": "dip_buying", "note": "Sin datos"}


def get_market_regime():
    """Devuelve el scoring_regime actual: BULL_QUIET | BULL_VOLATILE | BEAR_MODERATE | BEAR_PANIC.
    Cached junto con get_spy_regime. Usado por fetch_ticker para pesos adaptativos.
    """
    now = time.monotonic()
    with _spy_unified_lock:
        if _spy_unified_cache["scoring_regime"] is not None and (now - _spy_unified_cache["ts"]) < _REGIME_TTL:
            return _spy_unified_cache["scoring_regime"]
    _refresh_spy_cache()
    with _spy_unified_lock:
        return _spy_unified_cache["scoring_regime"] or "BULL_VOLATILE"


def get_vix_sma5():
    """Devuelve el VIX suavizado (SMA5). None si no hay datos.
    Usa el cache unificado — no hace request extra a yfinance.
    """
    now = time.monotonic()
    with _spy_unified_lock:
        if _spy_unified_cache["regime"] is not None and (now - _spy_unified_cache["ts"]) < _REGIME_TTL:
            return _spy_unified_cache["regime"].get("vix_sma5")
    _refresh_spy_cache()
    with _spy_unified_lock:
        reg = _spy_unified_cache.get("regime") or {}
        return reg.get("vix_sma5")



def is_momentum_candidate(row):
    """
    Criterios de entrada para modo momentum (mercado alcista).
    El activo debe mostrar fuerza real, no ser una caída que se compra.
      - Tendencia alcista:  SMA50 > SMA200 (trend == "bullish")
      - RSI entre 45 y 68: ni sobrecomprado ni en caída libre
      - Fuerza relativa positiva vs SPY (rel_strength > 0)
      - No está en máximos extremos: BB% < 88 (evitar entrar en la cresta)
      - Retorno 1 mes positivo o levemente negativo (> -3%): confirma momentum
    """
    trend        = row.get("trend")
    rsi          = row.get("rsi")
    rel_strength = row.get("rel_strength")
    bb_pct       = row.get("bb_pct")
    ret_1m       = row.get("ret_1m")

    if trend != "bullish":              return False
    if rsi is None or not (45 <= rsi <= 68): return False
    if rel_strength is None or rel_strength <= 0: return False
    if bb_pct is not None and bb_pct > 88:  return False
    if ret_1m is not None and ret_1m < -3:  return False
    return True


def calc_relative_strength(ticker_ret_14d, ticker_beta, spy_returns):
    """Fuerza relativa del ticker vs SPY ajustada por beta.
    Positivo = supera al mercado. Negativo = cae más de lo esperado.
    Ej: SPY -5%, AAPL(β1.2) -8% → esperado -6%, relativo = -8%-(-6%) = -2%"""
    if spy_returns is None or ticker_ret_14d is None:
        return None
    spy_ret = spy_returns.get("ret_14d")
    if spy_ret is None:
        return None
    beta = ticker_beta if (ticker_beta is not None and math.isfinite(ticker_beta)) else 1.0
    expected = spy_ret * beta
    return round(ticker_ret_14d - expected, 2)

# ── CHART/DIST CACHES (thread-safe) ──────────────────────────────────────────
_CHART_CACHE: dict = {}
_DIST_CACHE:  dict = {}
_chart_lock = threading.Lock()
_dist_lock  = threading.Lock()

def cache_get(store, lock, ticker):
    with lock:
        entry = store.get(ticker)
        if entry and (time.monotonic() - entry["ts"]) < CHART_TTL:
            return entry["data"]
    return None

def cache_set(store, lock, ticker, data):
    with lock:
        if len(store) >= CHART_MAX and ticker not in store:
            oldest = min(store, key=lambda k: store[k]["ts"])
            del store[oldest]
        store[ticker] = {"ts": time.monotonic(), "data": data}

def chart_cache_get(ticker):  return cache_get(_CHART_CACHE, _chart_lock, ticker)
def chart_cache_set(ticker, data): cache_set(_CHART_CACHE, _chart_lock, ticker, data)
def dist_cache_get(ticker):   return cache_get(_DIST_CACHE, _dist_lock, ticker)
def dist_cache_set(ticker, data):  cache_set(_DIST_CACHE, _dist_lock, ticker, data)

# ── FETCH TICKER (main data function) ────────────────────────────────────────



def fetch_ticker(name, ticker):
    """Fetch all indicators for a single ticker. Returns dict or None."""
    try:
        t   = yf.Ticker(ticker)
        now = datetime.now()
        _hist_raw = t.history(period="1y", auto_adjust=True)
        if _hist_raw.empty:
            return None
        _hist_raw.index = _hist_raw.index.tz_localize(None) if _hist_raw.index.tzinfo else _hist_raw.index
        _keep = [c for c in ["Open","High","Low","Close","Volume"] if c in _hist_raw.columns]
        hist = _hist_raw[_keep].copy()
        del _hist_raw

        close = hist["Close"]
        price = float(close.iloc[-1])
        last_bar = hist.index[-1]

        # Long-term returns (5y) — cached 24h, sin HTTP extra por ciclo
        _hist5y = _get_hist5y(ticker, t)
        hist_3y = _hist5y if _hist5y is not None else hist[["Close"]]

        def closest(delta_days, h=None):
            src = h if h is not None else hist
            target = now - timedelta(days=delta_days)
            subset = src[src.index <= target]
            if not subset.empty:
                v = float(subset["Close"].iloc[-1])
                return v if math.isfinite(v) else None
            if h is not None:
                subset2 = hist[hist.index <= target]
                if not subset2.empty:
                    v = float(subset2["Close"].iloc[-1])
                    return v if math.isfinite(v) else None
            return None

        prev_close = float(close.iloc[-2]) if len(close) >= 2 else None
        week_ago   = closest(7)
        month_ago  = closest(30)
        ytd_start  = hist[hist.index <= datetime(now.year, 1, 1)]
        ytd_price  = float(ytd_start["Close"].iloc[-1]) if not ytd_start.empty else None
        year_ago   = closest(365, hist_3y)
        three_yr   = closest(1095, hist_3y)

        # Prob alcista 30d
        prob_up_30d = None
        try:
            cl3 = hist_3y["Close"]
            if len(cl3) >= 60:
                wins  = sum(1 for i in range(len(cl3) - 30) if float(cl3.iloc[i+30]) > float(cl3.iloc[i]))
                total = len(cl3) - 30
                prob_up_30d = round(wins / total * 100, 1) if total > 0 else None
        except Exception:
            pass
        del hist_3y

        # 52-week range
        hi52 = float(hist["High"].iloc[-252:].max())
        lo52 = float(hist["Low"].iloc[-252:].min())
        rng  = hi52 - lo52
        pos52 = round((price - lo52) / rng * 100) if (rng and not math.isnan(hi52) and not math.isnan(lo52)) else None

        # RSI (14)
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi_s = 100 - (100 / (1 + rs))
        _rsi_v = rsi_s.iloc[-1]
        rsi = round(float(_rsi_v), 1) if (not rsi_s.empty and not math.isnan(float(_rsi_v))) else None

        # SMA 50/200
        sma50  = float(close.rolling(50).mean().iloc[-1])  if len(close) >= 50  else None
        sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
        if sma50 and sma200:
            trend = "bullish" if sma50 > sma200 else "bearish"
        else:
            trend = None

        # Bollinger Bands (20, 2σ)
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        bb_up = float((sma20 + 2 * std20).iloc[-1]) if len(close) >= 20 else None
        bb_lo = float((sma20 - 2 * std20).iloc[-1]) if len(close) >= 20 else None
        if bb_up and bb_lo and bb_up != bb_lo:
            bb_pct = round((price - bb_lo) / (bb_up - bb_lo) * 100, 1)
        else:
            bb_pct = None

        # Intraday
        r15, r60, r180, up_vol, dn_vol, last_price = fetch_intraday(ticker)

        # Price range prediction
        base = last_price if last_price else price
        if up_vol is not None and dn_vol is not None:
            price_hi = round(base * (1 + up_vol / 100), 2)
            price_lo = round(base * (1 - dn_vol / 100), 2)
            range_up = f"+{up_vol:.2f}%"
            range_dn = f"-{dn_vol:.2f}%"
        else:
            price_hi = price_lo = None
            range_up = range_dn = None

        # Volume relative
        vol_rel = None
        try:
            if "Volume" in hist.columns:
                vol_s   = hist["Volume"].replace(0, float("nan"))
                vol_day = float(vol_s.iloc[-1]) if not vol_s.empty else None
                vol_avg = float(vol_s.rolling(20).mean().iloc[-1]) if len(vol_s) >= 20 else None
                if vol_day and vol_avg and vol_avg > 0:
                    vol_rel = round(vol_day / vol_avg, 2)
        except Exception:
            pass

        # RSI Divergence
        rsi_divergence = None
        try:
            if len(rsi_s) >= 40 and rsi is not None:
                window    = 30
                rsi_w     = rsi_s.iloc[-window:]
                close_w   = close.iloc[-window:]
                prior_idx = rsi_w.iloc[:-3].idxmin()
                prior_rsi = float(rsi_s[prior_idx])
                prior_px  = float(close[prior_idx])
                current_rsi = float(rsi_s.iloc[-1])
                current_px  = float(close.iloc[-1])
                if current_px < prior_px and current_rsi > prior_rsi + 2:
                    rsi_divergence = "bullish"
                elif current_px > prior_px and current_rsi < prior_rsi - 2:
                    rsi_divergence = "bearish"
        except Exception:
            pass

        # Fundamentals
        pe_ratio, div_yield, beta = get_fundamentals(ticker)

        # ── Relative Strength vs SPY (beta-adjusted) ─────────────────────
        # Calcula si el ticker cae más o menos de lo esperado vs el mercado
        rel_strength = None
        if ticker not in ("SPY", "^GSPC"):
            ret_14d = None
            if len(close) > 14:
                p14 = float(close.iloc[-15])
                if p14 > 0:
                    ret_14d = (price - p14) / p14 * 100
            spy_rets = get_spy_returns()
            rel_strength = calc_relative_strength(ret_14d, beta, spy_rets)

        # ── Signal ────────────────────────────────────────────────────────
        score = 0
        if rsi is not None:
            if rsi <= 30:   score += 3
            elif rsi <= 40: score += 2
            elif rsi <= 45: score += 1
            elif rsi >= 70: score -= 3
            elif rsi >= 60: score -= 2
            elif rsi >= 55: score -= 1
        if bb_pct is not None:
            if bb_pct <= 15:   score += 2
            elif bb_pct <= 30: score += 1
            elif bb_pct >= 85: score -= 2
            elif bb_pct >= 70: score -= 1
        if trend == "bullish": score += 1
        elif trend == "bearish": score -= 1

        rsi_ok = rsi is not None and rsi <= 30
        if rsi_ok and score >= 5:     signal = "strong_buy"
        elif score >= 3:              signal = "buy"
        elif score >= 1:              signal = "watch"
        elif score <= -5:             signal = "strong_avoid"
        elif score <= -3:             signal = "caution"
        else:                         signal = "neutral"

        # ── Investment Score (1-100) ──────────────────────────────────────
        sc_rsi = sc_bb = sc_trend = sc_vol = sc_ret1m = 0
        sc_pe = sc_dy = sc_div_bonus = 0
        sc_rsi_note = sc_bb_note = sc_trend_note = sc_vol_note = ""
        sc_div_note = sc_pe_note = sc_ret1m_note = sc_dy_note = ""

        # RSI — 30 pts
        if rsi is not None:
            if   rsi <= 20: sc_rsi = 30; sc_rsi_note = f"RSI {rsi:.0f} — sobreventa extrema (+30)"
            elif rsi <= 30: sc_rsi = 26; sc_rsi_note = f"RSI {rsi:.0f} — sobreventa fuerte (+26)"
            elif rsi <= 40: sc_rsi = 20; sc_rsi_note = f"RSI {rsi:.0f} — sobreventa moderada (+20)"
            elif rsi <= 50: sc_rsi = 15; sc_rsi_note = f"RSI {rsi:.0f} — neutral-bajo (+15)"
            elif rsi <= 60: sc_rsi = 10; sc_rsi_note = f"RSI {rsi:.0f} — neutral (+10)"
            elif rsi <= 70: sc_rsi =  5; sc_rsi_note = f"RSI {rsi:.0f} — sobrecompra leve (+5)"
            else:           sc_rsi =  0; sc_rsi_note = f"RSI {rsi:.0f} — sobrecompra (+0)"

        # BB% — 25 pts
        if bb_pct is not None:
            if   bb_pct <=  5: sc_bb = 25; sc_bb_note = f"BB% {bb_pct:.0f} — precio en suelo (+25)"
            elif bb_pct <= 20: sc_bb = 21; sc_bb_note = f"BB% {bb_pct:.0f} — banda inferior (+21)"
            elif bb_pct <= 35: sc_bb = 16; sc_bb_note = f"BB% {bb_pct:.0f} — zona baja (+16)"
            elif bb_pct <= 65: sc_bb = 11; sc_bb_note = f"BB% {bb_pct:.0f} — zona media (+11)"
            elif bb_pct <= 80: sc_bb =  5; sc_bb_note = f"BB% {bb_pct:.0f} — zona alta (+5)"
            else:              sc_bb =  1; sc_bb_note = f"BB% {bb_pct:.0f} — banda superior (+1)"

        # Trend — 18 pts
        if trend == "bullish":
            sc_trend = 18; sc_trend_note = "Tendencia alcista SMA50>SMA200 (+18)"
        elif trend == "bearish":
            sc_trend =  3; sc_trend_note = "Tendencia bajista SMA50<SMA200 (+3)"
        else:
            sc_trend =  9; sc_trend_note = "Tendencia sin determinar (+9)"

        # Volume — 10 pts
        if vol_rel is not None:
            if   vol_rel >= 2.0: sc_vol = 10; sc_vol_note = f"Volumen {vol_rel:.1f}× — confirma señal (+10)"
            elif vol_rel >= 1.5: sc_vol =  8; sc_vol_note = f"Volumen {vol_rel:.1f}× — alto (+8)"
            elif vol_rel >= 0.8: sc_vol =  5; sc_vol_note = f"Volumen {vol_rel:.1f}× — normal (+5)"
            else:                sc_vol =  2; sc_vol_note = f"Volumen {vol_rel:.1f}× — bajo (+2)"
        else:
            sc_vol = 6; sc_vol_note = "Volumen sin datos — neutral (+6)"

        # Ret 1M — 7 pts
        ret_1m_val = pct(price, month_ago)
        if ret_1m_val is not None:
            if   ret_1m_val <= -10: sc_ret1m = 7; sc_ret1m_note = f"Caída 1M {ret_1m_val:.1f}% — oportunidad (+7)"
            elif ret_1m_val <=  -5: sc_ret1m = 5; sc_ret1m_note = f"Caída 1M {ret_1m_val:.1f}% (+5)"
            elif ret_1m_val <=   0: sc_ret1m = 3; sc_ret1m_note = f"Retorno 1M {ret_1m_val:.1f}% — plano (+3)"
            elif ret_1m_val <=   5: sc_ret1m = 2; sc_ret1m_note = f"Subida 1M {ret_1m_val:.1f}% (+2)"
            else:                   sc_ret1m = 1; sc_ret1m_note = f"Subida fuerte 1M {ret_1m_val:.1f}% (+1)"
        else:
            sc_ret1m = 3; sc_ret1m_note = "Retorno 1M sin datos (+3)"

        # Quality filters (−15 max)
        sc_quality = 0
        quality_flags = []
        ret_1w_val = pct(price, week_ago)
        if ret_1w_val is not None and ret_1w_val <= -12:
            sc_quality -= 8
            quality_flags.append(f"Caída 1W {ret_1w_val:.1f}% — cuchillo cayendo (−8)")
        elif ret_1w_val is not None and ret_1w_val <= -8:
            sc_quality -= 4
            quality_flags.append(f"Caída 1W {ret_1w_val:.1f}% — momentum muy negativo (−4)")
        if vol_rel is not None and vol_rel < 0.5:
            sc_quality -= 5
            quality_flags.append(f"Volumen {vol_rel:.1f}× — sin interés comprador (−5)")
        if pos52 is not None and pos52 <= 10 and trend == "bearish":
            sc_quality -= 5
            quality_flags.append(f"Mínimos 52s ({pos52}%) + tendencia bajista (−5)")
        sc_quality = max(-15, sc_quality)
        sc_quality_note = " | ".join(quality_flags) if quality_flags else "Sin señales de trampa de valor (0)"

        # Fundamentals (±10)
        if pe_ratio is not None and pe_ratio > 0:
            if   pe_ratio < 12:  sc_pe =  5; sc_pe_note = f"P/E {pe_ratio} — muy barato (+5)"
            elif pe_ratio < 18:  sc_pe =  3; sc_pe_note = f"P/E {pe_ratio} — razonable (+3)"
            elif pe_ratio < 25:  sc_pe =  1; sc_pe_note = f"P/E {pe_ratio} — algo caro (+1)"
            elif pe_ratio < 35:  sc_pe = -2; sc_pe_note = f"P/E {pe_ratio} — caro (−2)"
            else:                sc_pe = -4; sc_pe_note = f"P/E {pe_ratio} — muy caro (−4)"
        else:
            sc_pe = 0; sc_pe_note = "P/E sin datos — no afecta (0)"

        if div_yield is not None and div_yield > 0:
            if   div_yield >= 4: sc_dy =  5; sc_dy_note = f"Dividendo {div_yield:.2f}% — muy atractivo (+5)"
            elif div_yield >= 2: sc_dy =  3; sc_dy_note = f"Dividendo {div_yield:.2f}% — bueno (+3)"
            elif div_yield >= 1: sc_dy =  1; sc_dy_note = f"Dividendo {div_yield:.2f}% — modesto (+1)"
            else:                sc_dy = -1; sc_dy_note = f"Dividendo {div_yield:.2f}% — casi nulo (−1)"
        else:
            sc_dy = 0; sc_dy_note = "Sin dividendo — no afecta (0)"

        # RSI Divergence (±8)
        if rsi_divergence == "bullish":
            sc_div_bonus =  8; sc_div_note = "Divergencia RSI alcista (+8)"
        elif rsi_divergence == "bearish":
            sc_div_bonus = -8; sc_div_note = "Divergencia RSI bajista (−8)"
        else:
            sc_div_bonus =  0; sc_div_note = "Sin divergencia RSI (0)"

        # ── Relative Strength vs SPY (±10 pts) ───────────────────────────
        # Distingue "cae porque todo cae" de "cae más de lo esperado"
        # rel_strength > 0 → supera al mercado (bueno para contrarian: el rebote es real)
        # rel_strength < 0 → cae más que el mercado (malo: debilidad genuina)
        sc_rel = 0
        sc_rel_note = ""
        if rel_strength is not None:
            if   rel_strength <= -8:  sc_rel = -10; sc_rel_note = f"vs SPY {rel_strength:+.1f}% — muy débil vs mercado (−10)"
            elif rel_strength <= -4:  sc_rel =  -6; sc_rel_note = f"vs SPY {rel_strength:+.1f}% — débil vs mercado (−6)"
            elif rel_strength <= -2:  sc_rel =  -3; sc_rel_note = f"vs SPY {rel_strength:+.1f}% — algo débil (−3)"
            elif rel_strength <=  2:  sc_rel =   0; sc_rel_note = f"vs SPY {rel_strength:+.1f}% — en línea con mercado (0)"
            elif rel_strength <=  5:  sc_rel =   4; sc_rel_note = f"vs SPY {rel_strength:+.1f}% — supera al mercado (+4)"
            elif rel_strength <=  8:  sc_rel =   7; sc_rel_note = f"vs SPY {rel_strength:+.1f}% — muy fuerte vs mercado (+7)"
            else:                     sc_rel =  10; sc_rel_note = f"vs SPY {rel_strength:+.1f}% — líder de mercado (+10)"
        else:
            sc_rel_note = "Sin datos relativos vs SPY (0)"

        # ── Pesos adaptativos por regimen de mercado ──────────────────────────
        # El umbral de entrada (85) no cambia — cambian los pesos de cada componente.
        # En BULL_QUIET: BB% y fuerza relativa valen mas (RSI<=30 es raro en bull tranquilo).
        # En BEAR_PANIC: RSI y divergencia dominan; tendencia y PE casi no aportan.
        # Los tickers con score>=85 con pesos fijos siempre seguiran activandose.
        # Solo la zona gris 78-84 puede cruzar el umbral con el regimen correcto.
        _sr = get_market_regime()
        if _sr == "BULL_QUIET":
            # Bull tranquilo (SPY>SMA50, VIX<18): mas senales, enfasis en tecnico suave
            sc_rsi      = round(sc_rsi      * 1.10)
            sc_bb       = round(sc_bb       * 1.30)
            sc_trend    = round(sc_trend    * 1.10)
            sc_vol      = round(sc_vol      * 1.20)
            sc_rel      = round(sc_rel      * 1.30)
            sc_ret1m    = round(sc_ret1m    * 0.90)
            sc_quality  = round(sc_quality  * 0.85)  # penalizacion de calidad algo menor
            sc_pe       = round(sc_pe       * 1.10)
            sc_div_bonus= round(sc_div_bonus* 1.20)
            _regime_label = "BULL_QUIET"
        elif _sr == "BEAR_MODERATE":
            # Correccion/bear temprano (SPY<SMA50, VIX 18-30): mas exigente en RSI y calidad
            sc_rsi      = round(sc_rsi      * 1.30)
            sc_bb       = round(sc_bb       * 0.90)
            sc_trend    = round(sc_trend    * 0.75)
            sc_vol      = round(sc_vol      * 1.10)
            sc_rel      = round(sc_rel      * 1.20)
            sc_ret1m    = round(sc_ret1m    * 1.20)
            sc_quality  = round(sc_quality  * 1.40)  # filtros de calidad muy estrictos
            sc_pe       = round(sc_pe       * 0.85)
            sc_div_bonus= round(sc_div_bonus* 1.30)
            _regime_label = "BEAR_MODERATE"
        elif _sr == "BEAR_PANIC":
            # Panico/crash (VIX>30): solo RSI extremo y divergencia cuentan de verdad
            sc_rsi      = round(sc_rsi      * 1.50)
            sc_bb       = round(sc_bb       * 0.80)
            sc_trend    = round(sc_trend    * 0.55)
            sc_vol      = round(sc_vol      * 1.40)
            sc_rel      = round(sc_rel      * 0.75)
            sc_ret1m    = round(sc_ret1m    * 1.30)
            sc_quality  = round(sc_quality  * 1.60)  # maxima penalizacion por trampas
            sc_pe       = round(sc_pe       * 0.65)
            sc_div_bonus= round(sc_div_bonus* 1.50)
            _regime_label = "BEAR_PANIC"
        else:
            # BULL_VOLATILE (SPY>SMA50, VIX 18-28): pesos estándar — sin multiplicadores
            # Los valores de sc_* ya calculados arriba son los pesos finales
            _regime_label = "BULL_VOLATILE"

        # Total — re-aplicar cap de calidad tras multiplicadores adaptativos
        # (los multiplicadores pueden llevar sc_quality más allá de -15)
        sc_quality = max(-15, sc_quality)
        inv_score_raw = sc_rsi + sc_bb + sc_trend + sc_vol + sc_ret1m + sc_pe + sc_dy + sc_div_bonus + sc_quality + sc_rel
        inv_score = max(1, min(100, inv_score_raw))

        inv_score_breakdown = {
            "signal":  signal, "rsi": sc_rsi_note, "bb": sc_bb_note,
            "trend":   sc_trend_note, "vol": sc_vol_note, "ret1m": sc_ret1m_note,
            "quality": sc_quality_note, "rel": sc_rel_note,
            "pe": sc_pe_note, "dy": sc_dy_note,
            "div":     sc_div_note, "total": inv_score,
            "regime":  _regime_label,
            # Nota del régimen para el frontend — explica por qué cambió el score
            "regime_note": {
                "BULL_QUIET":    "Pesos adaptativos: BB% y Rel.Strength aumentados",
                "BULL_VOLATILE": "Pesos estándar",
                "BEAR_MODERATE": "Pesos adaptativos: RSI y Calidad aumentados",
                "BEAR_PANIC":    "Pesos adaptativos: solo RSI extremo cuenta",
            }.get(_regime_label, ""),
        }

        # Sparkline
        spark_raw = close.iloc[-30:].tolist() if len(close) >= 30 else close.tolist()
        s_min, s_max = min(spark_raw), max(spark_raw)
        sparkline = [round((v - s_min) / (s_max - s_min) * 100, 1) for v in spark_raw] if s_max > s_min else [50.0] * len(spark_raw)

        # Precalentar cache ATR usando el hist ya descargado — evita request extra en paper_engine
        calc_atr_pct(ticker, hist=hist)

        # Market open
        bar_age_hours = (now - last_bar).total_seconds() / 3600
        market_open = bar_age_hours < 1.0

        # FX conversion
        fx = get_eurusd() if ticker in USD_TICKERS else 1.0
        def conv(v):
            return round(v * fx, 4) if v is not None else None

        return {
            "name": name, "ticker": ticker,
            "price": round(price * fx, 2),
            "low52": round(lo52 * fx, 2), "high52": round(hi52 * fx, 2), "pos52": pos52,
            "rsi": rsi, "rsi_divergence": rsi_divergence,
            "trend": trend, "bb_pct": bb_pct,
            "signal": signal, "inv_score": inv_score,
            "inv_score_breakdown": inv_score_breakdown,
            "vol_rel": vol_rel, "pe_ratio": pe_ratio, "div_yield": div_yield,
            "sparkline": sparkline,
            "range_up": range_up, "range_dn": range_dn,
            "price_hi": conv(price_hi), "price_lo": conv(price_lo),
            "ret_15m": r15, "ret_1h": r60, "ret_3h": r180,
            "ret_1d": pct(price, prev_close), "ret_1w": pct(price, week_ago),
            "ret_1m": pct(price, month_ago), "ret_ytd": pct(price, ytd_price),
            "ret_1y": pct(price, year_ago), "ret_3y": pct(price, three_yr),
            "beta": beta, "prob_up_30d": prob_up_30d,
            "rel_strength": rel_strength,
            "market_open": market_open,
            "currency": "EUR" if fx != 1.0 else "local",
        }
    except Exception as e:
        import traceback
        log.warning("Error fetching %s: %s | %s", ticker, e, traceback.format_exc().split("\n")[-3])
        return None

# ── BREAKDOWN TO STRING ───────────────────────────────────────────────────────
def breakdown_to_str(bd):
    if not bd or not isinstance(bd, dict):
        return str(bd)
    sig = bd.get("signal", "")
    label = "★ COMPRA FUERTE" if sig == "strong_buy" else sig.upper()
    regime = bd.get("regime", "BULL_VOLATILE")
    regime_icons = {
        "BULL_QUIET":    "🟢 BULL QUIET (VIX<18)",
        "BULL_VOLATILE": "🟡 BULL VOLATILE",
        "BEAR_MODERATE": "🟠 BEAR MODERATE",
        "BEAR_PANIC":    "🔴 BEAR PANIC (VIX>30)",
    }
    sep = "─────────────────────"
    lines = [label, f"Régimen: {regime_icons.get(regime, regime)}", sep]
    for k in ("rsi", "bb", "trend", "vol", "ret1m", "quality", "rel", "pe", "dy", "div"):
        v = bd.get(k, "")
        if v:
            lines.append(v)
    lines += [sep, f"Total: {bd.get('total', '?')}/100"]
    return "\n".join(lines)
