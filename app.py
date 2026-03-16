import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"]      = "1"
os.environ["OMP_NUM_THREADS"]      = "1"
os.environ["NUMEXPR_NUM_THREADS"]  = "1"

from flask import Flask, render_template, jsonify, request
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import threading
import math
import time
import gc
import os
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

# Gzip: reduce el payload de /api/data ~70% (76 tickers → ~400KB → ~60KB)
try:
    from flask_compress import Compress
    app.config["COMPRESS_ALGORITHM"]   = "gzip"
    app.config["COMPRESS_LEVEL"]       = 6
    app.config["COMPRESS_MIN_SIZE"]    = 500
    Compress(app)
    print("[boot] flask-compress: gzip activo")
except ImportError:
    print("[boot] flask-compress no instalado — ejecuta: pip install flask-compress")

# ── EMAIL CONFIG ───────────────────────────────────────────────────────────────
# Fill these in to receive alerts. Use a Gmail App Password (not your real password).
# Guide: myaccount.google.com → Security → 2-Step Verification → App Passwords
EMAIL_FROM     = ""        # e.g. "tumail@gmail.com"
EMAIL_PASSWORD = ""        # Gmail App Password (16 chars)
EMAIL_TO       = ""        # where to send alerts (can be same address)
EMAIL_ENABLED  = False     # set True once you fill in the above

# Track which tickers already sent alert this session (avoid spam)
alerted = set()


# ── TICKERS ────────────────────────────────────────────────────────────────────
GROUPS = {
    "MAJOR INDICES": [
        ("S&P 500",            "^GSPC"),
        ("MSCI World",         "URTH"),
        ("NASDAQ Composite",   "^IXIC"),
        ("Euro STOXX 50",      "^STOXX50E"),
        ("MSCI Emerging Mkts", "EEM"),
        ("Russell 2000",       "IWM"),
        ("S&P 500 Eq. Weight", "RSP"),
        ("VIX",                "^VIX"),
    ],
    "MAG 7": [
        ("NVIDIA",    "NVDA"),
        ("Apple",     "AAPL"),
        ("Alphabet",  "GOOGL"),
        ("Microsoft", "MSFT"),
        ("Amazon",    "AMZN"),
        ("Meta",      "META"),
        ("Tesla",     "TSLA"),
    ],
    "BONDS": [
        ("Long Duration US Bonds", "TLT"),
        ("Short Duration US Bonds","SHY"),
        ("US Agg Bond",           "AGG"),
        ("High Yield",            "HYG"),
        ("TIPS (Inflation)",      "TIP"),
    ],
    "COMMODITIES": [
        ("Oil (Brent)",   "BZ=F"),
        ("Oil (WTI)",     "CL=F"),
        ("Natural Gas",   "NG=F"),
        ("Gold",          "GC=F"),
        ("Silver",        "SI=F"),
        ("Copper",        "HG=F"),
        ("Platinum",      "PL=F"),
        ("Wheat",         "ZW=F"),
        ("Bitcoin",       "BTC-USD"),
        ("Ethereum",      "ETH-USD"),
    ],
    "CURRENCIES": [
        ("DXY (USD Index)", "DX-Y.NYB"),
        ("EUR/USD",         "EURUSD=X"),
        ("USD/JPY",         "JPY=X"),
        ("GBP/USD",         "GBPUSD=X"),
        ("USD/CHF",         "CHF=X"),
        ("USD/CNY",         "CNY=X"),
    ],
    "EUROPE": [
        ("UK (FTSE 100)",     "ISF.L"),
        ("France (CAC 40)",   "^FCHI"),
        ("Germany (DAX)",     "^GDAXI"),
        ("Netherlands (AEX)", "^AEX"),
        ("Spain (IBEX 35)",   "^SMSI"),
        ("Italy (FTSE MIB)",  "EWI"),
        ("Switzerland (SMI)", "^SSMI"),
    ],
    "ASIA": [
        ("Japan (Nikkei)",   "^N225"),
        ("South Korea",      "EWY"),
        ("India (Nifty)",    "^NSEI"),
        ("China",            "MCHI"),
        ("Hong Kong",        "^HSI"),
        ("Taiwan",           "EWT"),
        ("Vietnam",          "VNM"),
    ],
    "LATAM": [
        ("Brazil",    "EWZ"),
        ("Mexico",    "EWW"),
        ("Argentina", "ARGT"),
        ("Chile",     "ECH"),
        ("Peru",      "EPU"),
    ],
    "US SECTORS": [
        ("Technology",             "XLK"),
        ("Healthcare",             "XLV"),
        ("Financials",             "XLF"),
        ("Consumer Discret.",      "XLY"),
        ("Communication Svcs",     "XLC"),
        ("Industrials",            "XLI"),
        ("Consumer Staples",       "XLP"),
        ("Energy",                 "XLE"),
        ("Utilities",              "XLU"),
        ("Real Estate",            "XLRE"),
        ("Materials",              "XLB"),
    ],
    "EU SECTORS": [
        ("EU Banks",           "EUFN"),
        ("EU Healthcare",      "IXJ"),
        ("EU Industrials",     "EXV6.DE"),
        ("EU Energy",          "IXC"),
        ("EU Technology",      "IYW"),
        ("EU Consumer Staples","EXV5.DE"),
        ("EU Telecoms",        "IXP"),
        ("EU Utilities",       "JXI"),
        ("EU Materials",       "PDBC"),
        ("EU Real Estate",     "IPRP.L"),
    ],
}

cache = {"data": None, "last_updated": None}
lock  = threading.Lock()

# Fundamentals cache — refreshed every 60 min (P/E and div don't change per refresh cycle)
_fund_cache = {}
_fund_lock  = threading.Lock()
_FUND_TTL   = 3600

def get_fundamentals(ticker):
    now = time.time()
    with _fund_lock:
        c = _fund_cache.get(ticker)
        if c and (now - c["ts"]) < _FUND_TTL:
            return c["pe"], c["div"], c.get("beta")
    pe_ratio = div_yield = None
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
    # Beta from t.info (cached alongside PE/div)
    beta = None
    try:
        if 'full' not in dir():
            full = yf.Ticker(ticker).info
        beta_raw = full.get("beta")
        if beta_raw is not None and math.isfinite(float(beta_raw)):
            beta = round(float(beta_raw), 2)
    except Exception:
        pass
    with _fund_lock:
        _fund_cache[ticker] = {"pe": pe_ratio, "div": div_yield, "beta": beta, "ts": now}
    return pe_ratio, div_yield, beta

# ── DATA FETCH ─────────────────────────────────────────────────────────────────
def pct(new, old):
    if old and old != 0 and new:
        return round((new - old) / abs(old) * 100, 1)
    return None

_intraday_cache: dict = {}
_INTRADAY_TTL = 240  # 4 min — intraday no cambia más rápido que el refresco

def fetch_intraday(ticker):
    """Fetch short-term returns + hourly volatility. Single 1h call (was 2 calls)."""
    import time as _t
    _now = _t.monotonic()
    _hit = _intraday_cache.get(ticker)
    if _hit and (_now - _hit[0]) < _INTRADAY_TTL:
        return _hit[1]
    try:
        t = yf.Ticker(ticker)
        intra = t.history(period="5d", interval="1h", auto_adjust=True)
        if intra.empty or len(intra) < 2:
            return None, None, None, None, None, None
        intra.index = intra.index.tz_localize(None) if intra.index.tzinfo else intra.index

        now_price = float(intra["Close"].iloc[-1])
        now_time  = intra.index[-1]

        # Hourly volatility for range prediction
        hourly_rets = intra["Close"].pct_change().dropna()
        hourly_vol  = float(hourly_rets.std()) if len(hourly_rets) >= 5 else None

        # Momentum bias from last 3 hours
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

        # Short-term returns from 1h bars (approximate 15m/1h/3h)
        def price_h_ago(hours):
            target = now_time - timedelta(hours=hours)
            subset = intra[intra.index <= target]
            return float(subset["Close"].iloc[-1]) if not subset.empty else None

        r15  = pct(now_price, price_h_ago(0.25))
        r60  = pct(now_price, price_h_ago(1))
        r180 = pct(now_price, price_h_ago(3))

        _res = (r15, r60, r180, up_range, dn_range, now_price)
        _intraday_cache[ticker] = (_t.monotonic(), _res)
        return _res
    except Exception:
        return None, None, None, None, None, None

def fetch_ticker(name, ticker):
    try:
        t   = yf.Ticker(ticker)
        now = datetime.now()
        # 1y for indicators (RSI, SMA200, BB, etc.)
        hist = t.history(period="1y", auto_adjust=True)
        if hist.empty:
            return None
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index

        close  = hist["Close"]
        price  = float(close.iloc[-1])
        last_bar = hist.index[-1]

        # Separate lightweight fetch for long-term returns (3y price only).
        # Descartamos todas las columnas salvo Close para reducir RAM por thread.
        try:
            hist_3y = t.history(period="3y", auto_adjust=True)
            if not hist_3y.empty:
                hist_3y.index = hist_3y.index.tz_localize(None) if hist_3y.index.tzinfo else hist_3y.index
                hist_3y = hist_3y[["Close"]]  # solo Close — libera Open/High/Low/Volume
        except Exception:
            hist_3y = hist[["Close"]]  # fallback to 1y

        def closest(delta_days, h=None):
            # Anchor from `now`. Try primary source, fallback to hist if empty.
            src    = h if h is not None else hist
            target = now - timedelta(days=delta_days)
            subset = src[src.index <= target]
            if not subset.empty:
                v = float(subset["Close"].iloc[-1])
                return v if math.isfinite(v) else None
            # Fallback: if h was provided but empty, try base hist
            if h is not None:
                subset2 = hist[hist.index <= target]
                if not subset2.empty:
                    v = float(subset2["Close"].iloc[-1])
                    return v if math.isfinite(v) else None
            return None

        # 1D: penultimate bar — always correct regardless of market hours
        prev_close = float(close.iloc[-2]) if len(close) >= 2 else None
        week_ago   = closest(7)
        month_ago  = closest(30)
        ytd_start  = hist[hist.index <= datetime(now.year, 1, 1)]
        ytd_price  = float(ytd_start["Close"].iloc[-1]) if not ytd_start.empty else None
        year_ago   = closest(365, hist_3y)
        three_yr   = closest(1095, hist_3y)
        del hist_3y  # liberar ~3y de OHLCV — ya no se necesita

        # Use full available window (up to 252 bars) — avoids NaN with 1y data
        hi52  = float(hist["High"].iloc[-252:].max())
        lo52  = float(hist["Low"].iloc[-252:].min())
        rng   = hi52 - lo52
        pos52 = round((price - lo52) / rng * 100) if (rng and not math.isnan(hi52) and not math.isnan(lo52)) else None

        # ── RSI (14) ──────────────────────────────────────────────────────────
        delta  = close.diff()
        gain   = delta.clip(lower=0).rolling(14).mean()
        loss   = (-delta.clip(upper=0)).rolling(14).mean()
        rs     = gain / loss.replace(0, float("nan"))
        rsi_s  = 100 - (100 / (1 + rs))
        _rsi_v = rsi_s.iloc[-1]
        rsi    = round(float(_rsi_v), 1) if (not rsi_s.empty and not math.isnan(float(_rsi_v))) else None

        # ── SMA 50 / 200 trend ────────────────────────────────────────────────
        sma50  = float(close.rolling(50).mean().iloc[-1])  if len(close) >= 50  else None
        sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
        if sma50 and sma200:
            trend = "bullish" if sma50 > sma200 else "bearish"
        else:
            trend = None

        # ── Bollinger Bands (20, 2σ) ──────────────────────────────────────────
        sma20  = close.rolling(20).mean()
        std20  = close.rolling(20).std()
        bb_up  = float((sma20 + 2 * std20).iloc[-1]) if len(close) >= 20 else None
        bb_lo  = float((sma20 - 2 * std20).iloc[-1]) if len(close) >= 20 else None
        if bb_up and bb_lo and bb_up != bb_lo:
            bb_pct = round((price - bb_lo) / (bb_up - bb_lo) * 100, 1)
        else:
            bb_pct = None

        # ── Intraday ──────────────────────────────────────────────────────────
        r15, r60, r180, up_vol, dn_vol, last_price = fetch_intraday(ticker)

        # ── 1H Price Range Prediction ─────────────────────────────────────────
        base = last_price if last_price else price
        if up_vol is not None and dn_vol is not None:
            price_hi = round(base * (1 + up_vol / 100), 2)
            price_lo = round(base * (1 - dn_vol / 100), 2)
            range_up = f"+{up_vol:.2f}%"
            range_dn = f"-{dn_vol:.2f}%"
        else:
            price_hi = price_lo = None
            range_up = range_dn = None

        # ── Volume relative to 20-day average ───────────────────────────────
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

        # ── RSI Divergence ────────────────────────────────────────────────────
        rsi_divergence = None
        try:
            if len(rsi_s) >= 40 and rsi is not None:
                window      = 30
                rsi_w       = rsi_s.iloc[-window:]
                close_w     = close.iloc[-window:]
                prior_idx   = rsi_w.iloc[:-3].idxmin()
                prior_rsi   = float(rsi_s[prior_idx])
                prior_px    = float(close[prior_idx])
                current_rsi = float(rsi_s.iloc[-1])
                current_px  = float(close.iloc[-1])
                if current_px < prior_px and current_rsi > prior_rsi + 2:
                    rsi_divergence = "bullish"
                elif current_px > prior_px and current_rsi < prior_rsi - 2:
                    rsi_divergence = "bearish"
        except Exception:
            pass

        # ── Fundamentals cached 1h ──────────────────────────────────────────
        pe_ratio, div_yield, beta = get_fundamentals(ticker)

        # ── Entry Signal (RSI + BB + Trend) ───────────────────────────────────
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

        # ── Investment Score (1-100) ──────────────────────────────────────────
        # DISEÑO: componentes técnicos obligatorios (RSI, BB%, Trend, Ret1M, Vol)
        # se normalizan sobre su máximo posible. Fundamentales (P/E, Div) son
        # opcionales: solo penalizan si están disponibles y son malos; si no hay
        # datos no afectan. Divergencia RSI es un bonus/malus puro al final.
        # Máximo alcanzable sin fundamentales ≈ 95-100, con fundamentales ≈ 100.

        sc_rsi = sc_bb = sc_trend = sc_vol = sc_ret1m = 0
        sc_pe = sc_dy = sc_div_bonus = 0
        sc_rsi_note = sc_bb_note = sc_trend_note = sc_vol_note = ""
        sc_div_note = sc_pe_note = sc_ret1m_note = sc_dy_note = ""

        # ── Técnicos (base 90 pts posibles) ─────────────────────────────────
        # RSI — 30 pts max
        if rsi is not None:
            if   rsi <= 20: sc_rsi = 30; sc_rsi_note = f"RSI {rsi:.0f} — sobreventa extrema (+30)"
            elif rsi <= 30: sc_rsi = 26; sc_rsi_note = f"RSI {rsi:.0f} — sobreventa fuerte (+26)"
            elif rsi <= 40: sc_rsi = 20; sc_rsi_note = f"RSI {rsi:.0f} — sobreventa moderada (+20)"
            elif rsi <= 50: sc_rsi = 15; sc_rsi_note = f"RSI {rsi:.0f} — neutral-bajo (+15)"
            elif rsi <= 60: sc_rsi = 10; sc_rsi_note = f"RSI {rsi:.0f} — neutral (+10)"
            elif rsi <= 70: sc_rsi =  5; sc_rsi_note = f"RSI {rsi:.0f} — sobrecompra leve (+5)"
            else:           sc_rsi =  0; sc_rsi_note = f"RSI {rsi:.0f} — sobrecompra (+0)"

        # BB% — 25 pts max
        if bb_pct is not None:
            if   bb_pct <=  5: sc_bb = 25; sc_bb_note = f"BB% {bb_pct:.0f} — precio en suelo (+25)"
            elif bb_pct <= 20: sc_bb = 21; sc_bb_note = f"BB% {bb_pct:.0f} — banda inferior (+21)"
            elif bb_pct <= 35: sc_bb = 16; sc_bb_note = f"BB% {bb_pct:.0f} — zona baja (+16)"
            elif bb_pct <= 65: sc_bb = 11; sc_bb_note = f"BB% {bb_pct:.0f} — zona media (+11)"
            elif bb_pct <= 80: sc_bb =  5; sc_bb_note = f"BB% {bb_pct:.0f} — zona alta (+5)"
            else:              sc_bb =  1; sc_bb_note = f"BB% {bb_pct:.0f} — banda superior (+1)"

        # Trend SMA50/200 — 18 pts max
        if trend == "bullish":
            sc_trend = 18; sc_trend_note = "Tendencia alcista SMA50>SMA200 (+18)"
        elif trend == "bearish":
            sc_trend =  3; sc_trend_note = "Tendencia bajista SMA50<SMA200 (+3)"
        else:
            sc_trend =  9; sc_trend_note = "Tendencia sin determinar (+9)"

        # Volumen relativo — 10 pts max
        if vol_rel is not None:
            if   vol_rel >= 2.0: sc_vol = 10; sc_vol_note = f"Volumen {vol_rel:.1f}× — confirma señal (+10)"
            elif vol_rel >= 1.5: sc_vol =  8; sc_vol_note = f"Volumen {vol_rel:.1f}× — alto (+8)"
            elif vol_rel >= 0.8: sc_vol =  5; sc_vol_note = f"Volumen {vol_rel:.1f}× — normal (+5)"
            else:                sc_vol =  2; sc_vol_note = f"Volumen {vol_rel:.1f}× — bajo (+2)"
        else:
            sc_vol = 6; sc_vol_note = "Volumen sin datos — neutral (+6)"

        # Ret 1M — 7 pts max
        ret_1m_val = pct(price, month_ago)
        if ret_1m_val is not None:
            if   ret_1m_val <= -10: sc_ret1m = 7; sc_ret1m_note = f"Caída 1M {ret_1m_val:.1f}% — oportunidad (+7)"
            elif ret_1m_val <=  -5: sc_ret1m = 5; sc_ret1m_note = f"Caída 1M {ret_1m_val:.1f}% (+5)"
            elif ret_1m_val <=   0: sc_ret1m = 3; sc_ret1m_note = f"Retorno 1M {ret_1m_val:.1f}% — plano (+3)"
            elif ret_1m_val <=   5: sc_ret1m = 2; sc_ret1m_note = f"Subida 1M {ret_1m_val:.1f}% (+2)"
            else:                   sc_ret1m = 1; sc_ret1m_note = f"Subida fuerte 1M {ret_1m_val:.1f}% (+1)"
        else:
            sc_ret1m = 3; sc_ret1m_note = "Retorno 1M sin datos (+3)"

        # ── Fundamentales OPCIONALES (±10 pts) ──────────────────────────────
        # Solo suman si son buenos, solo penalizan si son malos.
        # Sin datos = 0 (no penaliza ni premia).
        # P/E: ±5 pts
        if pe_ratio is not None and pe_ratio > 0:
            if   pe_ratio < 12:  sc_pe =  5; sc_pe_note = f"P/E {pe_ratio} — muy barato (+5)"
            elif pe_ratio < 18:  sc_pe =  3; sc_pe_note = f"P/E {pe_ratio} — razonable (+3)"
            elif pe_ratio < 25:  sc_pe =  1; sc_pe_note = f"P/E {pe_ratio} — algo caro (+1)"
            elif pe_ratio < 35:  sc_pe = -2; sc_pe_note = f"P/E {pe_ratio} — caro (−2)"
            else:                sc_pe = -4; sc_pe_note = f"P/E {pe_ratio} — muy caro (−4)"
        else:
            sc_pe = 0; sc_pe_note = "P/E sin datos — no afecta (0)"

        # Dividendo: ±5 pts
        if div_yield is not None and div_yield > 0:
            if   div_yield >= 4: sc_dy =  5; sc_dy_note = f"Dividendo {div_yield:.2f}% — muy atractivo (+5)"
            elif div_yield >= 2: sc_dy =  3; sc_dy_note = f"Dividendo {div_yield:.2f}% — bueno (+3)"
            elif div_yield >= 1: sc_dy =  1; sc_dy_note = f"Dividendo {div_yield:.2f}% — modesto (+1)"
            else:                sc_dy = -1; sc_dy_note = f"Dividendo {div_yield:.2f}% — casi nulo (−1)"
        else:
            sc_dy = 0; sc_dy_note = "Sin dividendo — no afecta (0)"

        # ── Bonus divergencia RSI (±8 pts) ──────────────────────────────────
        if rsi_divergence == "bullish":
            sc_div_bonus =  8; sc_div_note = "Divergencia RSI alcista (+8)"
        elif rsi_divergence == "bearish":
            sc_div_bonus = -8; sc_div_note = "Divergencia RSI bajista (−8)"
        else:
            sc_div_bonus =  0; sc_div_note = "Sin divergencia RSI (0)"

        # ── Total ────────────────────────────────────────────────────────────
        # Técnicos (máx 90) + fundamentales (±10) + divergencia (±8) → clamp 1-100
        inv_score_raw = sc_rsi + sc_bb + sc_trend + sc_vol + sc_ret1m + sc_pe + sc_dy + sc_div_bonus
        inv_score = max(1, min(100, inv_score_raw))

        inv_score_breakdown = {
            "signal": signal,
            "rsi":    sc_rsi_note,
            "bb":     sc_bb_note,
            "trend":  sc_trend_note,
            "vol":    sc_vol_note,
            "ret1m":  sc_ret1m_note,
            "pe":     sc_pe_note,
            "dy":     sc_dy_note,
            "div":    sc_div_note,
            "total":  inv_score,
        }

        # ── Prob alcista 30d (from hist_3y rolling windows) ────────────────────
        prob_up_30d = None
        try:
            src_h = hist_3y if not hist_3y.empty else hist
            cl3   = src_h["Close"]
            if len(cl3) >= 60:
                wins  = sum(1 for i in range(len(cl3) - 30)
                            if float(cl3.iloc[i+30]) > float(cl3.iloc[i]))
                total = len(cl3) - 30
                prob_up_30d = round(wins / total * 100, 1) if total > 0 else None
        except Exception:
            pass

        # ── Sparkline (30 days of closes, normalised 0-100) ──────────────────
        spark_raw = close.iloc[-30:].tolist() if len(close) >= 30 else close.tolist()
        s_min, s_max = min(spark_raw), max(spark_raw)
        if s_max > s_min:
            sparkline = [round((v - s_min) / (s_max - s_min) * 100, 1) for v in spark_raw]
        else:
            sparkline = [50.0] * len(spark_raw)

        return {
            "name":          name,
            "ticker":        ticker,
            "price":         round(price, 2),
            "low52":         round(lo52, 2),
            "high52":        round(hi52, 2),
            "pos52":         pos52,
            "rsi":               rsi,
            "rsi_divergence":    rsi_divergence,
            "trend":             trend,
            "bb_pct":            bb_pct,
            "signal":            signal,
            "inv_score":         inv_score,
            "inv_score_breakdown": inv_score_breakdown,
            "vol_rel":           vol_rel,
            "pe_ratio":          pe_ratio,
            "div_yield":         div_yield,
            "sparkline":     sparkline,
            "range_up":      range_up,
            "range_dn":      range_dn,
            "price_hi":      price_hi,
            "price_lo":      price_lo,
            "ret_15m":       r15,
            "ret_1h":        r60,
            "ret_3h":        r180,
            "ret_1d":        pct(price, prev_close),
            "ret_1w":        pct(price, week_ago),
            "ret_1m":        pct(price, month_ago),
            "ret_ytd":       pct(price, ytd_price),
            "ret_1y":        pct(price, year_ago),
            "ret_3y":        pct(price, three_yr),
            "beta":          beta,
            "prob_up_30d":   prob_up_30d,
        }
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return None

def send_alert_email(strong_buys):
    """Send email listing all strong buy signals."""
    if not EMAIL_ENABLED or not strong_buys:
        return
    try:
        subject = f"★ Market Tracker — {len(strong_buys)} Compra Fuerte {'signal' if len(strong_buys)==1 else 'signals'}"
        rows = "".join([
            f"<tr><td style='padding:6px 12px;font-weight:600'>{r['name']}</td>"
            f"<td style='padding:6px 12px;color:#aaa'>{r['ticker']}</td>"
            f"<td style='padding:6px 12px'>RSI {r['rsi']}</td>"
            f"<td style='padding:6px 12px'>BB {r['bb_pct']}%</td></tr>"
            for r in strong_buys
        ])
        html = f"""
        <div style='font-family:monospace;background:#0d0f12;color:#c8cdd6;padding:24px;border-radius:8px'>
          <h2 style='color:#34b566;margin-bottom:16px'>★ COMPRA FUERTE — {datetime.now().strftime('%d %b %Y %H:%M')}</h2>
          <table style='border-collapse:collapse;width:100%'>
            <tr style='color:#555e6e;font-size:11px'>
              <th style='padding:6px 12px;text-align:left'>Nombre</th>
              <th style='padding:6px 12px;text-align:left'>Ticker</th>
              <th style='padding:6px 12px;text-align:left'>RSI</th>
              <th style='padding:6px 12px;text-align:left'>BB%</th>
            </tr>
            {rows}
          </table>
          <p style='color:#555e6e;font-size:11px;margin-top:16px'>
            Abre tu tracker para ver el detalle completo.
          </p>
        </div>"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_FROM, EMAIL_PASSWORD)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"[ALERT] Email sent: {len(strong_buys)} strong buy(s)")
    except Exception as e:
        print(f"[ALERT] Email failed: {e}")

def refresh_data():
    t0 = datetime.now()
    n  = sum(len(v) for v in GROUPS.values())
    print(f"[{t0.strftime('%H:%M:%S')}] Fetching {n} tickers in parallel…")

    # Flat list preserving group membership
    all_tasks = [(group, name, ticker)
                 for group, tickers in GROUPS.items()
                 for name, ticker in tickers]

    # Fetch all tickers concurrently — 8 workers: cap peak RAM en Railway (512MB)
    # Con 32 workers se acumulan ~32 DataFrames de 1y en memoria a la vez (~200MB peak).
    # Con 8 workers el peak baja a ~50MB; el ciclo tarda ~15s más pero cabe en free tier.
    row_map = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_ticker, name, ticker): (group, ticker)
                   for group, name, ticker in all_tasks}
        for fut in as_completed(futures):
            group, ticker = futures[fut]
            try:
                row = fut.result()
                if row:
                    row_map[ticker] = (group, row)
            except Exception as e:
                print(f"  [warn] {ticker}: {e}")

    # Reassemble preserving original group order
    result = {group: [] for group in GROUPS}
    strong_buys_this_cycle = []
    for group, name, ticker in all_tasks:
        if ticker in row_map:
            _, row = row_map[ticker]
            result[group].append(row)
            if row["signal"] == "strong_buy" and ticker not in alerted:
                strong_buys_this_cycle.append(row)
                alerted.add(ticker)
            elif row["signal"] != "strong_buy" and ticker in alerted:
                alerted.discard(ticker)

    with lock:
        cache["data"]         = result
        cache["last_updated"] = datetime.now().strftime("%d-%b-%y %H:%M")

    elapsed = (datetime.now() - t0).total_seconds()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Done in {elapsed:.1f}s.")

    # Purgar entradas expiradas del intraday cache (evita acumulación indefinida)
    _now_mono = time.monotonic()
    expired_intra = [k for k, v in list(_intraday_cache.items()) if (_now_mono - v[0]) > _INTRADAY_TTL * 3]
    for k in expired_intra:
        _intraday_cache.pop(k, None)
    if expired_intra:
        print(f"[cache] purgadas {len(expired_intra)} entradas intraday expiradas")
    if strong_buys_this_cycle:
        threading.Thread(target=send_alert_email, args=(strong_buys_this_cycle,), daemon=True).start()

def background_refresh():
    while True:
        refresh_data()
        # Actualizar papers en background tras cada ciclo de mercado
        with lock:
            market_data = cache.get("data") or {}
        if market_data:
            try:
                run_paper2_trading(market_data)
                print("[bg] paper2 actualizado")
            except Exception as e:
                print(f"[bg] paper2 error: {e}")
            try:
                run_paper4_trading(market_data)
                print("[bg] paper4 actualizado")
            except Exception as e:
                print(f"[bg] paper4 error: {e}")
        # Forzar GC tras el ciclo completo — devuelve RAM al OS inmediatamente
        # (Python no libera al OS por sí solo hasta el siguiente GC automático)
        gc.collect()
        time.sleep(300)


# ── ROUTES ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

# ══════════════════════════════════════════════════════════════════════════════
# ── PAPER TRADING 2 — Score > 85, salida automática a las 24h ────────────────
# ══════════════════════════════════════════════════════════════════════════════
PAPER2_FILE         = os.path.join(os.path.dirname(__file__), "paper2_trades.json")
PAPER2_INITIAL_CAP  = 10147.0
PAPER2_POSITION_PCT = 0.20   # 20% of capital per trade
PAPER2_MIN_SCORE    = 85     # minimum inv_score to open a position
PAPER2_HOLD_HOURS   = 24     # sell exactly 24h after entry

paper2_lock = threading.Lock()

def load_paper2():
    if os.path.exists(PAPER2_FILE):
        try:
            with open(PAPER2_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "capital":    PAPER2_INITIAL_CAP,
        "open":       [],
        "closed":     [],
        "equity_log": [],
        "cooldowns":  {},
    }

def save_paper2(data):
    with open(PAPER2_FILE, "w") as f:
        json.dump(data, f, indent=2)

def run_paper2_trading(market_data):
    """Score>85 strategy: buy on score, sell after 24h (or when positive if negative at 24h, stop loss -7%)."""
    with paper2_lock:
        pt = load_paper2()
        now_dt  = datetime.now()
        now_str = now_dt.strftime("%Y-%m-%d %H:%M")
        changed = False

        ticker_map = {}
        for group_rows in market_data.values():
            for row in group_rows:
                ticker_map[row["ticker"]] = row

        # ── Clean expired cooldowns ───────────────────────────────────────────
        if "cooldowns" not in pt:
            pt["cooldowns"] = {}
        expired = [t for t, until in pt["cooldowns"].items()
                   if datetime.strptime(until, "%Y-%m-%d %H:%M") <= now_dt]
        for t in expired:
            del pt["cooldowns"][t]

        # ── Check exits ──────────────────────────────────────────────────────
        # Rules:
        #   < 24h  → hold normally
        #   ≥ 24h + ret >= 0 → sell (24h elapsed and in profit or breakeven)
        #   ≥ 24h + ret < 0  → wait for positive OR stop loss -7% (48h cooldown)
        PAPER2_STOP_LOSS = -7.0

        still_open = []
        for pos in pt["open"]:
            ticker = pos["ticker"]
            row    = ticker_map.get(ticker)

            entry_dt   = datetime.strptime(pos["entry_date"], "%Y-%m-%d %H:%M")
            hours_held = (now_dt - entry_dt).total_seconds() / 3600
            current_price = row["price"] if row else pos.get("current_price", pos["entry_price"])
            ret_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100

            exit_reason = None
            needs_cooldown = False

            if hours_held >= PAPER2_HOLD_HOURS:
                if ret_pct >= 0:
                    exit_reason = f"24h cumplidas ({hours_held:.1f}h)"
                elif ret_pct <= PAPER2_STOP_LOSS:
                    exit_reason = f"Stop loss {ret_pct:.1f}% ({hours_held:.1f}h)"
                    needs_cooldown = True
                else:
                    pass  # still negative, waiting for recovery

            if exit_reason:
                pnl = pos["shares"] * (current_price - pos["entry_price"])
                pt["capital"] += pos["shares"] * current_price
                pt["closed"].append({
                    "ticker":       ticker,
                    "name":         pos["name"],
                    "entry_date":   pos["entry_date"],
                    "exit_date":    now_str,
                    "entry_price":  pos["entry_price"],
                    "exit_price":   round(current_price, 2),
                    "shares":       pos["shares"],
                    "ret_pct":      round(ret_pct, 2),
                    "pnl":          round(pnl, 2),
                    "reason":       exit_reason,
                    "entry_score":  pos.get("entry_score", "—"),
                    "hours_held":   round(hours_held, 1),
                })
                if needs_cooldown:
                    pt["cooldowns"][ticker] = (now_dt + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
                changed = True
            else:
                if row:
                    pos["current_price"] = round(current_price, 2)
                    pos["ret_pct"]       = round(ret_pct, 2)
                    pos["hours_held"]    = round(hours_held, 1)
                    pos["hours_left"]    = round(max(0, PAPER2_HOLD_HOURS - hours_held), 1)
                    pos["score"]         = row.get("inv_score")
                    pos["waiting_recovery"] = hours_held >= PAPER2_HOLD_HOURS and ret_pct < 0
                still_open.append(pos)

        pt["open"] = still_open

        # ── Check entries: score > 85 ─────────────────────────────────────────
        open_tickers = {p["ticker"] for p in pt["open"]}
        for ticker, row in ticker_map.items():
            if ticker in pt["cooldowns"]:
                continue
            score = row.get("inv_score")
            if score is None or score < PAPER2_MIN_SCORE:
                continue
            if ticker in open_tickers:
                continue
            position_size = pt["capital"] * PAPER2_POSITION_PCT
            if position_size < 1:
                continue
            shares = position_size / row["price"]
            pt["capital"] -= position_size
            pt["open"].append({
                "ticker":        ticker,
                "name":          row["name"],
                "entry_date":    now_str,
                "entry_price":   round(row["price"], 2),
                "current_price": round(row["price"], 2),
                "shares":        round(shares, 6),
                "ret_pct":       0.0,
                "hours_held":    0.0,
                "hours_left":    float(PAPER2_HOLD_HOURS),
                "entry_score":   score,
                "score":         score,
            })
            open_tickers.add(ticker)
            changed = True

        # ── Log equity ────────────────────────────────────────────────────────
        open_value = sum(
            p["shares"] * ticker_map.get(p["ticker"], {}).get("price", p["entry_price"])
            for p in pt["open"]
        )
        total_equity = round(pt["capital"] + open_value, 2)
        pt["equity_log"].append({"date": now_str, "equity": total_equity})
        pt["equity_log"] = pt["equity_log"][-500:]

        if changed or True:
            save_paper2(pt)

        return pt, ticker_map

@app.route("/paper2")
def paper2():
    return render_template("paper2.html")

@app.route("/api/paper2")
def api_paper2():
    with lock:
        market_data = cache.get("data") or {}
    if not market_data:
        return jsonify({"error": "no data yet"})
    pt, ticker_map = run_paper2_trading(market_data)

    open_value   = sum(p["shares"] * ticker_map.get(p["ticker"], {}).get("price", p["entry_price"]) for p in pt["open"])
    total_equity = round(pt["capital"] + open_value, 2)
    total_ret    = round((total_equity - PAPER2_INITIAL_CAP) / PAPER2_INITIAL_CAP * 100, 2)

    return jsonify({
        "capital":      round(pt["capital"], 2),
        "open_value":   round(open_value, 2),
        "total_equity": total_equity,
        "total_ret":    total_ret,
        "initial":      PAPER2_INITIAL_CAP,
        "open":         pt["open"],
        "closed":       list(reversed(pt["closed"])),
        "equity_log":   pt["equity_log"],
        "min_score":    PAPER2_MIN_SCORE,
        "hold_hours":   PAPER2_HOLD_HOURS,
    })

@app.route("/api/paper2/reset", methods=["POST"])
def api_paper2_reset():
    if os.path.exists(PAPER2_FILE):
        os.remove(PAPER2_FILE)
    return jsonify({"status": "reset"})

@app.route("/api/paper2/sell", methods=["POST"])
def api_paper2_sell():
    ticker = request.json.get("ticker")
    reason = request.json.get("reason", "Venta manual")
    if not ticker:
        return jsonify({"error": "no ticker"}), 400
    with paper2_lock:
        pt = load_paper2()
        with lock:
            market_data = cache.get("data") or {}
        ticker_map = {}
        for group_rows in market_data.values():
            for row in group_rows:
                ticker_map[row["ticker"]] = row
        pos = next((p for p in pt["open"] if p["ticker"] == ticker), None)
        if not pos:
            return jsonify({"error": "position not found"}), 404
        row = ticker_map.get(ticker)
        current_price = row["price"] if row else pos["entry_price"]
        ret_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
        pnl     = pos["shares"] * (current_price - pos["entry_price"])
        pt["capital"] += pos["shares"] * current_price
        pt["open"] = [p for p in pt["open"] if p["ticker"] != ticker]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        pt["closed"].append({
            "ticker":       ticker,
            "name":         pos["name"],
            "entry_date":   pos["entry_date"],
            "exit_date":    now_str,
            "entry_price":  pos["entry_price"],
            "exit_price":   round(current_price, 2),
            "shares":       pos["shares"],
            "ret_pct":      round(ret_pct, 2),
            "pnl":          round(pnl, 2),
            "reason":       reason,
            "entry_score":  pos.get("entry_score", "—"),
            "hours_held":   pos.get("hours_held", "—"),
        })
        save_paper2(pt)
    return jsonify({"status": "sold"})

# ── PAPER TRADING 4 — Combo Score ≥ 80 + Signal buy/strong_buy, salida 24h ───
# Entry:  inv_score ≥ 80 AND signal in (buy, strong_buy)
# Exit:   24h elapsed + ret ≥ 0% | stop loss -7% | take profit +10%
# Cooldown: 48h after stop loss or RSI exit before re-entering same ticker
PAPER4_FILE         = os.path.join(os.path.dirname(__file__), "paper4_trades.json")
PAPER4_INITIAL_CAP  = 10089.0
PAPER4_POSITION_PCT = 0.20
PAPER4_MIN_SCORE    = 80
PAPER4_TAKE_PROFIT  = 10.0
PAPER4_STOP_LOSS    = -7.0
PAPER4_HOLD_HOURS   = 24

paper4_lock = threading.Lock()

def load_paper4():
    if os.path.exists(PAPER4_FILE):
        try:
            with open(PAPER4_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"capital": PAPER4_INITIAL_CAP, "open": [], "closed": [], "equity_log": [], "cooldowns": {}}

def save_paper4(data):
    with open(PAPER4_FILE, "w") as f:
        json.dump(data, f)

def run_paper4_trading(market_data):
    """Combo Score≥80 + Signal, exit after 24h if positive (wait if negative, SL -7%, TP +10%).
    48h cooldown after stop loss exit."""
    with paper4_lock:
        pt = load_paper4()
        if "cooldowns" not in pt:
            pt["cooldowns"] = {}
        now_dt  = datetime.now()
        now_str = now_dt.strftime("%Y-%m-%d %H:%M")
        changed = False

        ticker_map = {}
        for group_rows in market_data.values():
            for row in group_rows:
                ticker_map[row["ticker"]] = row

        # ── Clean expired cooldowns ───────────────────────────────────────────
        expired = [t for t, until in pt["cooldowns"].items()
                   if datetime.strptime(until, "%Y-%m-%d %H:%M") <= now_dt]
        for t in expired:
            del pt["cooldowns"][t]
        if expired:
            changed = True

        # ── Check exits ───────────────────────────────────────────────────────
        COOLDOWN_HOURS = 48.0

        still_open = []
        for pos in pt["open"]:
            ticker = pos["ticker"]
            row    = ticker_map.get(ticker)

            entry_dt   = datetime.strptime(pos["entry_date"], "%Y-%m-%d %H:%M")
            hours_held = (now_dt - entry_dt).total_seconds() / 3600
            current_price = row["price"] if row else pos.get("current_price", pos["entry_price"])
            ret_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100

            exit_reason = None
            needs_cooldown = False

            if ret_pct >= PAPER4_TAKE_PROFIT:
                exit_reason = f"Take profit +{ret_pct:.1f}%"
            elif ret_pct <= PAPER4_STOP_LOSS:
                exit_reason = f"Stop loss {ret_pct:.1f}%"
                needs_cooldown = True
            elif hours_held >= PAPER4_HOLD_HOURS:
                if ret_pct >= 0:
                    exit_reason = f"24h cumplidas ({hours_held:.1f}h)"
                elif ret_pct <= PAPER4_STOP_LOSS:
                    exit_reason = f"Stop loss {ret_pct:.1f}% ({hours_held:.1f}h)"
                    needs_cooldown = True
                # else: still negative past 24h, waiting for recovery

            if exit_reason:
                pnl = pos["shares"] * (current_price - pos["entry_price"])
                pt["capital"] += pos["shares"] * current_price
                pt["closed"].append({
                    "ticker":        ticker,
                    "name":          pos["name"],
                    "entry_date":    pos["entry_date"],
                    "exit_date":     now_str,
                    "entry_price":   pos["entry_price"],
                    "exit_price":    round(current_price, 2),
                    "shares":        pos["shares"],
                    "ret_pct":       round(ret_pct, 2),
                    "pnl":           round(pnl, 2),
                    "reason":        exit_reason,
                    "entry_score":   pos.get("entry_score", "—"),
                    "entry_signal":  pos.get("entry_signal", "—"),
                    "hours_held":    round(hours_held, 1),
                })
                if needs_cooldown:
                    cooldown_until = (now_dt + timedelta(hours=COOLDOWN_HOURS)).strftime("%Y-%m-%d %H:%M")
                    pt["cooldowns"][ticker] = cooldown_until
                changed = True
            else:
                if row:
                    pos["current_price"] = round(current_price, 2)
                    pos["ret_pct"]       = round(ret_pct, 2)
                    pos["hours_held"]    = round(hours_held, 1)
                    pos["hours_left"]    = round(max(0, PAPER4_HOLD_HOURS - hours_held), 1)
                    pos["score"]         = row.get("inv_score")
                    pos["waiting_recovery"] = hours_held >= PAPER4_HOLD_HOURS and ret_pct < 0
                still_open.append(pos)

        pt["open"] = still_open

        # ── Check entries ─────────────────────────────────────────────────────
        open_tickers = {p["ticker"] for p in pt["open"]}
        for ticker, row in ticker_map.items():
            if ticker in pt["cooldowns"]:
                continue
            score  = row.get("inv_score")
            signal = row.get("signal", "")
            trend  = row.get("trend", "")
            if score is None or score < PAPER4_MIN_SCORE:
                continue
            if signal not in ("buy", "strong_buy"):
                continue
            if trend != "bullish":
                continue
            if ticker in open_tickers:
                continue
            position_size = pt["capital"] * PAPER4_POSITION_PCT
            if position_size < 1:
                continue
            shares = position_size / row["price"]
            pt["capital"] -= position_size
            pt["open"].append({
                "ticker":           ticker,
                "name":             row["name"],
                "entry_date":       now_str,
                "entry_price":      round(row["price"], 2),
                "current_price":    round(row["price"], 2),
                "shares":           round(shares, 6),
                "ret_pct":          0.0,
                "hours_held":       0.0,
                "hours_left":       float(PAPER4_HOLD_HOURS),
                "entry_score":      score,
                "entry_signal":     signal,
                "score":            score,
                "waiting_recovery": False,
            })
            open_tickers.add(ticker)
            changed = True

        # ── Equity log ────────────────────────────────────────────────────────
        open_value   = sum(p["shares"] * ticker_map.get(p["ticker"], {}).get("price", p["entry_price"]) for p in pt["open"])
        total_equity = round(pt["capital"] + open_value, 2)
        pt["equity_log"].append({"date": now_str, "equity": total_equity})
        pt["equity_log"] = pt["equity_log"][-500:]

        if changed or True:
            save_paper4(pt)

        return pt, ticker_map

@app.route("/paper4")
def paper4():
    return render_template("paper4.html")

@app.route("/api/paper4")
def api_paper4():
    with lock:
        market_data = cache.get("data") or {}
    if not market_data:
        return jsonify({"error": "no data yet"})
    pt, ticker_map = run_paper4_trading(market_data)

    open_value   = sum(p["shares"] * ticker_map.get(p["ticker"], {}).get("price", p["entry_price"]) for p in pt["open"])
    total_equity = round(pt["capital"] + open_value, 2)
    total_ret    = round((total_equity - PAPER4_INITIAL_CAP) / PAPER4_INITIAL_CAP * 100, 2)

    return jsonify({
        "capital":      round(pt["capital"], 2),
        "open_value":   round(open_value, 2),
        "total_equity": total_equity,
        "total_ret":    total_ret,
        "initial":      PAPER4_INITIAL_CAP,
        "open":         pt["open"],
        "closed":       list(reversed(pt["closed"])),
        "equity_log":   pt["equity_log"],
        "cooldowns":    pt.get("cooldowns", {}),
        "min_score":    PAPER4_MIN_SCORE,
        "hold_hours":   PAPER4_HOLD_HOURS,
    })

@app.route("/api/paper4/reset", methods=["POST"])
def api_paper4_reset():
    if os.path.exists(PAPER4_FILE):
        os.remove(PAPER4_FILE)
    return jsonify({"status": "reset"})

@app.route("/api/paper4/sell", methods=["POST"])
def api_paper4_sell():
    ticker = request.json.get("ticker")
    reason = request.json.get("reason", "Venta manual")
    if not ticker:
        return jsonify({"error": "no ticker"}), 400
    with paper4_lock:
        pt = load_paper4()
        with lock:
            market_data = cache.get("data") or {}
        ticker_map = {}
        for group_rows in market_data.values():
            for row in group_rows:
                ticker_map[row["ticker"]] = row
        pos = next((p for p in pt["open"] if p["ticker"] == ticker), None)
        if not pos:
            return jsonify({"error": "position not found"}), 404
        row = ticker_map.get(ticker)
        current_price = row["price"] if row else pos["entry_price"]
        ret_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
        pnl     = pos["shares"] * (current_price - pos["entry_price"])
        pt["capital"] += pos["shares"] * current_price
        pt["open"] = [p for p in pt["open"] if p["ticker"] != ticker]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        pt["closed"].append({
            "ticker":        ticker,
            "name":          pos["name"],
            "entry_date":    pos["entry_date"],
            "exit_date":     now_str,
            "entry_price":   pos["entry_price"],
            "exit_price":    round(current_price, 2),
            "shares":        pos["shares"],
            "ret_pct":       round(ret_pct, 2),
            "pnl":           round(pnl, 2),
            "reason":        reason,
            "entry_score":   pos.get("entry_score", "—"),
            "entry_signal":  pos.get("entry_signal", "—"),
            "hours_held":    pos.get("hours_held", "—"),
        })
        save_paper4(pt)
    return jsonify({"status": "sold"})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Force an immediate data refresh in background."""
    t = threading.Thread(target=refresh_data, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})

@app.route("/api/config/email", methods=["POST"])
def config_email():
    global EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO, EMAIL_ENABLED
    d = request.json
    EMAIL_FROM     = d.get("from", EMAIL_FROM)
    EMAIL_PASSWORD = d.get("password", EMAIL_PASSWORD)
    EMAIL_TO       = d.get("to", EMAIL_TO)
    EMAIL_ENABLED  = bool(EMAIL_FROM and EMAIL_PASSWORD and EMAIL_TO)
    return jsonify({"enabled": EMAIL_ENABLED})

@app.route("/api/config/email", methods=["GET"])
def get_email_config():
    return jsonify({"enabled": EMAIL_ENABLED, "to": EMAIL_TO, "from": EMAIL_FROM})


def _breakdown_to_str(bd):
    """Reconstruye el string de tooltip desde el dict compacto guardado en cache.
    El dict ocupa ~40% menos RAM que el string formateado (no repite señaladores)."""
    if not bd or not isinstance(bd, dict):
        return str(bd)
    sig = bd.get("signal", "")
    label = "★ COMPRA FUERTE" if sig == "strong_buy" else sig.upper()
    sep = "─────────────────────"
    lines = [label, sep]
    for k in ("rsi", "bb", "trend", "vol", "ret1m", "pe", "dy", "div"):
        v = bd.get(k, "")
        if v:
            lines.append(v)
    lines += [sep, f"Total: {bd.get('total', '?')}/100"]
    return "\n".join(lines)


@app.route("/api/data")
def api_data():
    with lock:
        data         = cache["data"]
        last_updated = cache["last_updated"]
    if not data:
        return jsonify({"error": "no data yet"}), 503
    # Reconstruir inv_score_breakdown como string sólo en el momento de enviar
    # (en cache se guarda como dict compacto para ahorrar RAM)
    import copy
    out = {}
    for group, rows in data.items():
        out[group] = []
        for row in rows:
            r = dict(row)
            bd = r.get("inv_score_breakdown")
            if isinstance(bd, dict):
                r["inv_score_breakdown"] = _breakdown_to_str(bd)
            out[group].append(r)
    resp = jsonify({"data": out, "last_updated": last_updated})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/chart/<ticker>")
def api_chart(ticker):
    """Return OHLCV + indicators for charting (last 12 months)."""
    try:
        t    = yf.Ticker(ticker)
        hist = t.history(period="2y", auto_adjust=True)
        if hist.empty:
            return jsonify({"error": "no data"}), 404
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index

        close = hist["Close"]

        # RSI 14
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi_s = 100 - (100 / (1 + rs))

        # Bollinger Bands 20
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        bb_up = sma20 + 2 * std20
        bb_lo = sma20 - 2 * std20

        # SMA 50 / 200
        sma50  = close.rolling(50).mean()
        sma200 = close.rolling(200).mean()

        # Compra Fuerte signal markers
        signals = []
        for i in range(len(hist)):
            r   = float(rsi_s.iloc[i])  if not rsi_s.isna().iloc[i]  else None
            bb  = None
            if not bb_up.isna().iloc[i] and not bb_lo.isna().iloc[i]:
                rng = float(bb_up.iloc[i]) - float(bb_lo.iloc[i])
                if rng > 0:
                    bb = (float(close.iloc[i]) - float(bb_lo.iloc[i])) / rng * 100
            s50  = float(sma50.iloc[i])  if not sma50.isna().iloc[i]  else None
            s200 = float(sma200.iloc[i]) if not sma200.isna().iloc[i] else None
            if r and bb and s50 and s200:
                if r <= 30 and bb <= 20 and s50 > s200:
                    ts = int(hist.index[i].timestamp())
                    signals.append({"time": ts, "price": round(float(close.iloc[i]), 4)})

        def series(s):
            out = []
            for i, v in s.items():
                if not (v != v):  # not NaN
                    out.append({"time": int(i.timestamp()), "value": round(float(v), 4)})
            return out

        candles = []
        for i in range(len(hist)):
            row = hist.iloc[i]
            candles.append({
                "time":  int(hist.index[i].timestamp()),
                "open":  round(float(row["Open"]),  4),
                "high":  round(float(row["High"]),  4),
                "low":   round(float(row["Low"]),   4),
                "close": round(float(row["Close"]), 4),
            })

        return jsonify({
            "ticker":  ticker,
            "candles": candles,
            "rsi":     series(rsi_s),
            "bb_up":   series(bb_up),
            "bb_lo":   series(bb_lo),
            "sma50":   series(sma50),
            "sma200":  series(sma200),
            "signals": signals,
        })
    except Exception as e:
        print(f"[chart] Error {ticker}: {e}")
        return jsonify({"error": str(e)}), 500


# ── STATIC FILE ROUTES ─────────────────────────────────────────────────────────
@app.route("/service-worker.js")
def service_worker():
    """Serve service worker from project root (needed for push notifications)."""
    from flask import Response
    sw_path = os.path.join(os.path.dirname(__file__), "service-worker.js")
    if not os.path.exists(sw_path):
        return Response("// service worker not found", mimetype="application/javascript"), 404
    with open(sw_path) as f:
        content = f.read()
    return Response(content, mimetype="application/javascript")

@app.route("/favicon.ico")
def favicon():
    """Return empty favicon to suppress 404 logs."""
    from flask import Response
    return Response(b"", mimetype="image/x-icon")


@app.route("/api/dist/<ticker>")
def api_dist(ticker):
    """Return 10-year historical return distribution for a ticker."""
    try:
        t    = yf.Ticker(ticker)
        hist = t.history(period="10y", auto_adjust=True)
        if hist.empty or len(hist) < 60:
            return jsonify({"error": "insufficient data"}), 404
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index

        close = hist["Close"]

        # ── Rolling 30-day forward returns ────────────────────────────────────
        # For each day i, return = (close[i+30] - close[i]) / close[i] * 100
        rets_30 = []
        for i in range(len(close) - 30):
            r = (float(close.iloc[i + 30]) - float(close.iloc[i])) / float(close.iloc[i]) * 100
            if math.isfinite(r):
                rets_30.append(round(r, 2))

        if not rets_30:
            return jsonify({"error": "no returns computed"}), 404

        rets_30.sort()
        n = len(rets_30)

        # ── Summary stats ─────────────────────────────────────────────────────
        prob_up   = round(sum(1 for r in rets_30 if r > 0) / n * 100, 1)
        prob_down = round(100 - prob_up, 1)
        median    = round(rets_30[n // 2], 2)
        mean      = round(sum(rets_30) / n, 2)
        var_95    = round(rets_30[int(n * 0.05)], 2)   # 5th percentile
        var_99    = round(rets_30[int(n * 0.01)], 2)   # 1st percentile
        best_30   = round(rets_30[-1], 2)
        worst_30  = round(rets_30[0], 2)

        # ── Histogram buckets (fixed -30% to +30%, 2% width) ─────────────────
        bucket_w   = 2.0
        buck_min   = -30.0
        buck_max   =  30.0
        n_buckets  = int((buck_max - buck_min) / bucket_w)
        buckets    = [0] * n_buckets
        for r in rets_30:
            idx = int((r - buck_min) / bucket_w)
            idx = max(0, min(n_buckets - 1, idx))
            buckets[idx] += 1
        # Normalize to percentage of total
        bucket_pct = [round(b / n * 100, 2) for b in buckets]
        bucket_labels = [round(buck_min + i * bucket_w, 0) for i in range(n_buckets)]

        # ── Seasonality: mean return by calendar month ────────────────────────
        # Use actual 1-month forward return anchored to each month-start
        monthly = {}
        for i in range(len(close) - 21):
            month = hist.index[i].month
            r = (float(close.iloc[i + 21]) - float(close.iloc[i])) / float(close.iloc[i]) * 100
            if math.isfinite(r):
                monthly.setdefault(month, []).append(r)

        seasonality = {}
        for m in range(1, 13):
            vals = monthly.get(m, [])
            if vals:
                seasonality[m] = {
                    "mean":   round(sum(vals) / len(vals), 2),
                    "prob_up": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
                    "n":      len(vals)
                }
            else:
                seasonality[m] = {"mean": None, "prob_up": None, "n": 0}

        # Beta — only available for individual stocks (MAG7 etc), None for indices/ETFs
        beta_dist = None
        try:
            info = t.info
            b = info.get("beta")
            if b is not None and math.isfinite(float(b)):
                beta_dist = round(float(b), 2)
        except Exception:
            pass

        return jsonify({
            "ticker":        ticker,
            "n_samples":     n,
            "years":         round(n / 252, 1),
            "prob_up":       prob_up,
            "prob_down":     prob_down,
            "median":        median,
            "mean":          mean,
            "var_95":        var_95,
            "var_99":        var_99,
            "best_30":       best_30,
            "worst_30":      worst_30,
            "buckets":       bucket_pct,
            "bucket_labels": bucket_labels,
            "seasonality":   seasonality,
            "beta":          beta_dist,
        })
    except Exception as e:
        print(f"[dist] Error {ticker}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/ping")
def api_ping():
    """Lightweight: ¿hay datos listos? Usado por el frontend en la carga inicial."""
    with lock:
        ready = cache.get("data") is not None
        ts    = cache.get("last_updated")
    from flask import Response
    import json as _json
    return Response(_json.dumps({"ready": ready, "last_updated": ts}),
                    mimetype="application/json",
                    headers={"Cache-Control": "no-store"})

@app.route("/api/wake", methods=["GET"])
def api_wake():
    """Endpoint GET para UptimeRobot."""
    with lock:
        last = cache.get("last_updated")
    if last:
        try:
            last_dt = datetime.strptime(last, "%d-%b-%y %H:%M")
            age_min = (datetime.now() - last_dt).total_seconds() / 60
            if age_min < 25:
                return jsonify({"status": "ok", "msg": f"datos frescos ({age_min:.0f} min)"})
        except Exception:
            pass
    t = threading.Thread(target=refresh_data, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})

# ── MAIN ───────────────────────────────────────────────────────────────────────
# Lanzar el thread de background al importar el módulo — necesario para gunicorn,
# que no ejecuta el bloque __main__. Se protege con un flag para evitar doble
# arranque si Flask usa use_reloader=True en desarrollo.
_bg_started = False
def _start_background():
    global _bg_started
    if not _bg_started:
        _bg_started = True
        t = threading.Thread(target=background_refresh, daemon=True)
        t.start()
        print("[boot] background_refresh thread arrancado")

_start_background()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)