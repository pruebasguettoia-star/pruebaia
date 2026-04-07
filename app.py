"""
app.py — Market Tracker (Flask).
Módulos: config, auth, alpaca_api, indicators, paper_engine, storage
"""
import sys
import os

# Deshabilitar caché .pyc — evita que Railway ejecute bytecode obsoleto
# de versiones anteriores cacheadas en el build layer o Volume.
sys.dont_write_bytecode = True

import logging

os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"]      = "1"
os.environ["OMP_NUM_THREADS"]      = "1"
os.environ["NUMEXPR_NUM_THREADS"]  = "1"

# ── Logging estructurado ───────────────────────────────────────────────────────
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tracker")

from flask import Flask, render_template, jsonify, request, Response
import yfinance as yf
import threading
import math
import time
import gc
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    GROUPS, REFRESH_INTERVAL, PAPER2_INITIAL_CAP, PAPER2_HOLD_HOURS, PAPER2_MIN_SCORE,
    alpaca_enabled, alpaca_url,
    CHART_TTL, NON_TRADEABLE, _USD_BASE, EU_TICKERS, FUTURES_TICKERS,
)
from auth import require_admin
import indicators
import paper_engine
import alpaca_api

# Set de todos los tickers conocidos — usado para validar /api/chart y /api/dist
_KNOWN_TICKERS = (
    {ticker for tickers in GROUPS.values() for _, ticker in tickers}
)
import storage
import telegram_alerts

app = Flask(__name__)

# ── Gzip ──────────────────────────────────────────────────────────────────────
try:
    from flask_compress import Compress
    app.config["COMPRESS_ALGORITHM"] = "gzip"
    app.config["COMPRESS_LEVEL"]     = 6
    app.config["COMPRESS_MIN_SIZE"]  = 500
    Compress(app)
    log.info("gzip activo")
except ImportError:
    pass

if alpaca_enabled():
    log.info("Alpaca activado — %s", alpaca_url())

from config import ADMIN_TOKEN as _ADMIN_TOKEN
if not _ADMIN_TOKEN:
    log.warning("ADMIN_TOKEN no configurado — endpoints admin bloqueados (añádelo en Railway Variables)")

# ── RW LOCK para cache (fix #6) ──────────────────────────────────────────────
# Permite lecturas concurrentes, escritura exclusiva
class RWLock:
    def __init__(self):
        self._lock = threading.Lock()
        self._readers = 0
        self._readers_lock = threading.Lock()
        self._write_lock = threading.Lock()

    def read_acquire(self):
        with self._readers_lock:
            self._readers += 1
            if self._readers == 1:
                self._write_lock.acquire()

    def read_release(self):
        with self._readers_lock:
            self._readers -= 1
            if self._readers == 0:
                self._write_lock.release()

    def write_acquire(self):
        self._write_lock.acquire()

    def write_release(self):
        self._write_lock.release()

cache = {"data": None, "last_updated": None, "last_refresh_ts": None}
cache_lock = RWLock()
alerted = set()
alerted_lock = threading.Lock()          # protege el set alerted (thread-safe)
_refresh_lock = threading.Lock()  # evita ejecuciones concurrentes de refresh_data


# ── DATA REFRESH ──────────────────────────────────────────────────────────────
def refresh_data():
    if not _refresh_lock.acquire(blocking=False):
        log.debug("refresh ya en curso — omitiendo llamada concurrente")
        return
    try:
        t0 = datetime.now()
        n  = sum(len(v) for v in GROUPS.values())
        log.info("Fetching %d tickers…", n)

        try:
            _fx = yf.Ticker("EURUSD=X").fast_info
            _rate = getattr(_fx, "last_price", None)
            if _rate and _rate > 0:
                indicators.set_eurusd(round(1 / _rate, 6))
        except Exception as e:
            log.warning("fx error: %s", e)

        all_tasks = [(g, name, ticker) for g, tickers in GROUPS.items() for name, ticker in tickers]
        row_map = {}
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(indicators.fetch_ticker, name, ticker): (g, ticker) for g, name, ticker in all_tasks}
            for fut in as_completed(futures):
                g, ticker = futures[fut]
                try:
                    row = fut.result()
                    if row: row_map[ticker] = (g, row)
                except Exception as e:
                    log.warning("fetch %s: %s", ticker, e)

        result = {g: [] for g in GROUPS}
        strong_buys = []
        for g, name, ticker in all_tasks:
            if ticker in row_map:
                _, row = row_map[ticker]
                # Precalcular breakdown a string una sola vez en el ciclo de refresh
                bd = row.get("inv_score_breakdown")
                if isinstance(bd, dict):
                    row = dict(row)
                    row["inv_score_breakdown"] = indicators.breakdown_to_str(bd)
                result[g].append(row)
                with alerted_lock:
                    if row["signal"] == "strong_buy" and ticker not in alerted:
                        strong_buys.append(row)
                        alerted.add(ticker)
                    elif row["signal"] != "strong_buy" and ticker in alerted:
                        alerted.discard(ticker)

        cache_lock.write_acquire()
        try:
            cache["data"] = result
            cache["last_updated"] = datetime.now().strftime("%d-%b-%y %H:%M")
            cache["last_refresh_ts"] = time.time()
        finally:
            cache_lock.write_release()

        elapsed = (datetime.now() - t0).total_seconds()
        log.info("Done in %.1fs.", elapsed)
        indicators.purge_intraday_cache()
        # Persistir alerted para sobrevivir reinicios sin alertas duplicadas
        with alerted_lock:
            try:
                storage.save_alerted(set(alerted))
            except Exception as e:
                log.warning("No se pudo persistir alerted: %s", e)
        if strong_buys:
            threading.Thread(target=telegram_alerts.alert_strong_buys, args=(strong_buys,), daemon=True).start()
    finally:
        _refresh_lock.release()


def _refresh_sleep_seconds():
    """Devuelve los segundos a esperar según horario de mercado.
    - Mercado abierto (NYSE/NASDAQ): REFRESH_INTERVAL (5 min)
    - Fuera de mercado o fin de semana: 30 min — los datos no cambian
    """
    now_utc = datetime.utcnow()
    if now_utc.weekday() >= 5:  # sábado o domingo
        return 1800
    t = now_utc.hour * 60 + now_utc.minute
    # NYSE/NASDAQ: 13:30–21:00 UTC (cubre ET invierno y verano)
    # Añadimos 30 min de margen pre/post mercado para capturar movimientos
    if 13 * 60 <= t <= 21 * 60 + 30:
        return REFRESH_INTERVAL
    return 1800  # fuera de mercado: refrescar cada 30 min


def background_refresh():
    # Init paper engine (migra JSON → SQLite si existe)
    try:
        paper_engine.init()
    except Exception as e:
        log.critical("paper_engine.init() falló: %s — el hilo de fondo continúa sin trading", e)

    # Cargar alerted persistido para no re-alertar tras reinicio
    global alerted
    try:
        loaded = storage.get_alerted()
        with alerted_lock:
            alerted = loaded
        log.info("alerted cargado desde DB: %d tickers", len(loaded))
    except Exception as e:
        log.warning("No se pudo cargar alerted desde DB: %s", e)

    _last_weekly_date = None  # para enviar el informe solo una vez por lunes

    while True:
        try:
            t_start = time.monotonic()
            refresh_data()
            cache_lock.read_acquire()
            try:
                market_data = cache.get("data") or {}
            finally:
                cache_lock.read_release()
            if market_data:
                try:
                    paper_engine.run_trading(market_data)
                    log.debug("paper2 actualizado")
                except Exception as e:
                    log.error("paper2 error: %s", e)
            gc.collect()

            # ── Informe semanal — lunes entre 08:00 y 09:00 UTC ──────────────
            now_utc = datetime.utcnow()
            today   = now_utc.date()
            if (now_utc.weekday() == 0 and 8 <= now_utc.hour < 9
                    and _last_weekly_date != today):
                _last_weekly_date = today
                try:
                    cache_lock.read_acquire()
                    try:
                        _mdata = cache.get("data") or {}
                    finally:
                        cache_lock.read_release()
                    _positions = storage.get_open_positions()
                    _cap       = storage.get_capital()
                    _stats     = storage.get_performance_stats()
                    _vix       = indicators.get_vix_sma5()
                    _regime    = indicators.get_spy_regime().get("regime", "—")
                    _tmap      = {r["ticker"]: r for rows in _mdata.values() for r in rows}
                    # Top 5 tickers por score para el informe
                    _all_rows  = [r for rows in _mdata.values() for r in rows]
                    _top       = sorted(
                        [r for r in _all_rows if isinstance(r.get("inv_score"), (int, float))],
                        key=lambda r: r["inv_score"], reverse=True
                    )[:5]
                    threading.Thread(
                        target=telegram_alerts.send_weekly_report,
                        args=(_positions, _cap, _stats, _vix, _regime, _top),
                        daemon=True,
                    ).start()
                    log.info("Informe semanal enviado")
                except Exception as e:
                    log.error("Error enviando informe semanal: %s", e)

            sleep_secs = _refresh_sleep_seconds()
            elapsed    = time.monotonic() - t_start
            time.sleep(max(0, sleep_secs - elapsed))
        except Exception as e:
            log.critical("background_refresh error inesperado: %s — reintentando en 60s", e)
            time.sleep(60)


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES ────────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/paper2")
def paper2_page():
    return render_template("paper2.html")

# ── DATA ──────────────────────────────────────────────────────────────────────
@app.route("/api/data")
def api_data():
    cache_lock.read_acquire()
    try:
        data = cache["data"]
        last_updated = cache["last_updated"]
        refresh_ts = cache.get("last_refresh_ts")
    finally:
        cache_lock.read_release()

    if not data:
        return jsonify({"error": "no data yet"}), 503

    out = {}
    for group, rows in data.items():
        out[group] = []
        for row in rows:
            r = dict(row)
            # inv_score_breakdown ya viene como string desde refresh_data
            out[group].append(r)

    # Cooldowns from SQLite
    cooldowns = {}
    try:
        cd = storage.get_cooldowns()
        now_dt = datetime.now()
        for ticker, until_str in cd.items():
            until_dt = datetime.strptime(until_str, "%Y-%m-%d %H:%M")
            hours_left = (until_dt - now_dt).total_seconds() / 3600
            if hours_left > 0:
                cooldowns[ticker] = round(hours_left, 1)
    except Exception:
        pass

    # Data freshness (fix #12)
    freshness = None
    if refresh_ts:
        age_sec = time.time() - refresh_ts
        freshness = {
            "age_seconds": round(age_sec),
            "age_human": f"{int(age_sec // 60)}m {int(age_sec % 60)}s",
            "stale": age_sec > REFRESH_INTERVAL * 2,
        }

    resp = jsonify({
        "data": out, "last_updated": last_updated,
        "cooldowns": cooldowns, "freshness": freshness,
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp

# ── PAPER TRADING ─────────────────────────────────────────────────────────────
@app.route("/api/paper2")
def api_paper2():
    cache_lock.read_acquire()
    try:
        market_data = cache.get("data") or {}
    finally:
        cache_lock.read_release()
    if not market_data:
        return jsonify({"error": "no data yet"})
    return jsonify(paper_engine.get_read_only_state(market_data))

@app.route("/api/paper2/reset", methods=["POST"])
@require_admin
def api_paper2_reset():
    paper_engine.reset()
    return jsonify({"status": "reset"})

@app.route("/api/paper2/sell", methods=["POST"])
@require_admin
def api_paper2_sell():
    ticker = request.json.get("ticker")
    if not ticker:
        return jsonify({"error": "no ticker"}), 400
    cache_lock.read_acquire()
    try:
        market_data = cache.get("data") or {}
    finally:
        cache_lock.read_release()
    result = paper_engine.sell_manual(ticker, market_data)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)

# ── UX: PERFORMANCE STATS (fix #12) ──────────────────────────────────────────
@app.route("/api/paper2/stats")
def api_paper2_stats():
    """Estadísticas de rendimiento: global, mensual, semanal, razones de salida."""
    return jsonify(storage.get_performance_stats())

@app.route("/api/paper2/trades")
def api_paper2_trades():
    """Trades filtrados por periodo. ?from=2026-01-01&to=2026-03-31"""
    from_date = request.args.get("from", "2000-01-01")
    to_date   = request.args.get("to")
    trades = storage.get_trades_by_period(from_date, to_date)
    return jsonify({"trades": trades, "count": len(trades)})

# ── UX: EXPORT CSV (fix #12) ─────────────────────────────────────────────────
@app.route("/api/paper2/export")
def api_paper2_export():
    """Descarga historial de trades como CSV."""
    csv_data = storage.export_trades_csv()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades_history.csv"}
    )

# ── REFRESH ───────────────────────────────────────────────────────────────────
_last_manual_refresh = 0.0
_manual_refresh_lock = threading.Lock()
_MANUAL_REFRESH_MIN_INTERVAL = 30  # segundos entre refreshes manuales

# Rate limit para /api/chart y /api/dist — evita hammering con tickers arbitrarios
_chart_rate: dict = {}   # ticker → last_request_ts
_chart_rate_lock = threading.Lock()
_CHART_RATE_INTERVAL = 10  # segundos mínimos entre requests del mismo ticker

def _chart_rate_ok(ticker: str) -> bool:
    now = time.time()
    with _chart_rate_lock:
        last = _chart_rate.get(ticker, 0)
        if now - last < _CHART_RATE_INTERVAL:
            return False
        _chart_rate[ticker] = now
        # limpiar entradas antiguas (>1h) para no acumular tickers arbitrarios
        if len(_chart_rate) > 500:
            cutoff = now - 3600
            for k in [k for k, v in _chart_rate.items() if v < cutoff]:
                _chart_rate.pop(k, None)
        return True

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    global _last_manual_refresh
    with _manual_refresh_lock:
        now = time.time()
        if now - _last_manual_refresh < _MANUAL_REFRESH_MIN_INTERVAL:
            remaining = int(_MANUAL_REFRESH_MIN_INTERVAL - (now - _last_manual_refresh))
            return jsonify({"status": "rate_limited", "retry_after": remaining}), 429
        _last_manual_refresh = now
    t = threading.Thread(target=refresh_data, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})

# ── PROTECTED ─────────────────────────────────────────────────────────────────
@app.route("/api/backup")
@require_admin
def api_backup():
    import zipfile, io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Export trades as CSV
        zf.writestr("trades_history.csv", storage.export_trades_csv())
        # Export full state as JSON
        state = {
            "capital": storage.get_capital(),
            "open": storage.get_open_positions(),
            "closed": storage.get_closed_trades(9999),
            "equity_log": storage.get_equity_log(9999),
            "cooldowns": storage.get_cooldowns(),
        }
        zf.writestr("paper2_state.json", json.dumps(state, indent=2))
    buf.seek(0)
    return Response(buf.read(), mimetype="application/zip",
                    headers={"Content-Disposition": "attachment; filename=paper_backup.zip"})

# ── ALPACA ────────────────────────────────────────────────────────────────────
@app.route("/api/alpaca/status")
def api_alpaca_status():
    if not alpaca_enabled():
        return jsonify({"enabled": False, "msg": "Configura ALPACA_API_KEY y ALPACA_SECRET_KEY"})
    acct = alpaca_api.get_account()
    if not acct:
        return jsonify({"enabled": True, "error": "No se pudo conectar"})
    return jsonify({
        "enabled": True, "paper": "paper" in alpaca_url(),
        "equity": acct.get("equity"), "cash": acct.get("cash"),
        "buying_power": acct.get("buying_power"), "status": acct.get("status"),
    })

@app.route("/api/alpaca/positions")
def api_alpaca_positions():
    if not alpaca_enabled():
        return jsonify({"enabled": False})
    positions = alpaca_api.get_positions()
    return jsonify({"enabled": True, "positions": positions or []})

# ── CHART / DIST ──────────────────────────────────────────────────────────────
@app.route("/api/chart/<ticker>")
def api_chart(ticker):
    if ticker not in _KNOWN_TICKERS:
        return jsonify({"error": "ticker desconocido"}), 404
    cached = indicators.chart_cache_get(ticker)
    if cached: return jsonify(cached)
    if not _chart_rate_ok(ticker):
        return jsonify({"error": "rate_limited", "retry_after": _CHART_RATE_INTERVAL}), 429
    try:
        import pandas as pd
        t = yf.Ticker(ticker)
        hist = t.history(period="2y", auto_adjust=True)
        if hist.empty: return jsonify({"error": "no data"}), 404
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
        close = hist["Close"]

        # ── Indicadores vectorizados ──────────────────────────────────────────
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi_s = 100 - (100 / (1 + gain / loss.replace(0, float("nan"))))
        sma20  = close.rolling(20).mean()
        bb_up  = sma20 + 2 * close.rolling(20).std()
        bb_lo  = sma20 - 2 * close.rolling(20).std()
        sma50  = close.rolling(50).mean()
        sma200 = close.rolling(200).mean()

        # ── Señales de compra vectorizadas ────────────────────────────────────
        bb_rng   = (bb_up - bb_lo).replace(0, float("nan"))
        bb_pct_s = (close - bb_lo) / bb_rng * 100
        sig_mask = (
            rsi_s.notna() & bb_pct_s.notna() & sma50.notna() & sma200.notna() &
            (rsi_s <= 30) & (bb_pct_s <= 20) & (sma50 > sma200)
        )
        ts_arr = hist.index.astype("int64") // 10**9
        signals = [
            {"time": int(ts), "price": round(float(px), 4)}
            for ts, px, ok in zip(ts_arr, close, sig_mask)
            if ok
        ]

        # ── Series para el gráfico ────────────────────────────────────────────
        def series(s):
            return [
                {"time": int(ts), "value": round(float(v), 4)}
                for ts, v in zip(ts_arr, s)
                if v == v  # filtra NaN
            ]

        # ── Velas — vectorizado con itertuples ────────────────────────────────
        has_vol = "Volume" in hist.columns
        candles = []
        for row in hist.itertuples():
            vol = int(row.Volume) if (has_vol and row.Volume == row.Volume and row.Volume > 0) else None
            candles.append({
                "time":  int(row.Index.timestamp()),
                "open":  round(float(row.Open),  4),
                "high":  round(float(row.High),  4),
                "low":   round(float(row.Low),   4),
                "close": round(float(row.Close), 4),
                "volume": vol,
            })

        result = {
            "ticker": ticker, "candles": candles, "rsi": series(rsi_s),
            "bb_up": series(bb_up), "bb_lo": series(bb_lo),
            "sma50": series(sma50), "sma200": series(sma200), "signals": signals,
        }
        indicators.chart_cache_set(ticker, result)
        resp = jsonify(result)
        resp.headers["Cache-Control"] = f"public, max-age={int(CHART_TTL)}"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/dist/<ticker>")
def api_dist(ticker):
    if ticker not in _KNOWN_TICKERS:
        return jsonify({"error": "ticker desconocido"}), 404
    cached = indicators.dist_cache_get(ticker)
    if cached: return jsonify(cached)
    if not _chart_rate_ok(ticker):
        return jsonify({"error": "rate_limited", "retry_after": _CHART_RATE_INTERVAL}), 429
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="10y", auto_adjust=True)
        if hist.empty or len(hist) < 60: return jsonify({"error": "insufficient data"}), 404
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
        close = hist["Close"]
        rets = sorted(
            round((float(close.iloc[i+30]) - float(close.iloc[i])) / float(close.iloc[i]) * 100, 2)
            for i in range(len(close) - 30)
            if float(close.iloc[i]) != 0
            and math.isfinite((float(close.iloc[i+30]) - float(close.iloc[i])) / float(close.iloc[i]) * 100)
        )
        if not rets: return jsonify({"error": "no returns"}), 404
        n = len(rets)
        bw = 2.0; bmin = -30.0; bmax = 30.0; nb = int((bmax - bmin) / bw)
        buckets = [0] * nb
        for r in rets:
            idx = max(0, min(nb - 1, int((r - bmin) / bw))); buckets[idx] += 1
        monthly = {}
        for i in range(len(close) - 21):
            m = hist.index[i].month
            r = (float(close.iloc[i+21]) - float(close.iloc[i])) / float(close.iloc[i]) * 100
            if math.isfinite(r): monthly.setdefault(m, []).append(r)
        seasonality = {}
        for m in range(1, 13):
            vals = monthly.get(m, [])
            seasonality[m] = {"mean": round(sum(vals)/len(vals), 2) if vals else None,
                "prob_up": round(sum(1 for v in vals if v > 0)/len(vals)*100, 1) if vals else None, "n": len(vals)}
        beta = None
        try:
            b = t.info.get("beta")
            if b and math.isfinite(float(b)): beta = round(float(b), 2)
        except Exception:
            pass
        result = {"ticker": ticker, "n_samples": n, "years": round(n/252, 1),
            "prob_up": round(sum(1 for r in rets if r > 0)/n*100, 1),
            "prob_down": round(sum(1 for r in rets if r <= 0)/n*100, 1),
            "median": round(rets[n//2], 2), "mean": round(sum(rets)/n, 2),
            "var_95": round(rets[int(n*0.05)], 2), "var_99": round(rets[int(n*0.01)], 2),
            "best_30": round(rets[-1], 2), "worst_30": round(rets[0], 2),
            "buckets": [round(b/n*100, 2) for b in buckets],
            "bucket_labels": [round(bmin + i * bw, 0) for i in range(nb)],
            "seasonality": seasonality, "beta": beta}
        indicators.dist_cache_set(ticker, result)
        resp = jsonify(result)
        resp.headers["Cache-Control"] = f"public, max-age={int(CHART_TTL)}"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── UTILITY ───────────────────────────────────────────────────────────────────
@app.route("/api/health")
def api_health():
    """Healthcheck detallado para Railway y monitoreo externo."""
    cache_lock.read_acquire()
    try:
        ready = cache.get("data") is not None
        ts = cache.get("last_updated")
        refresh_ts = cache.get("last_refresh_ts")
    finally:
        cache_lock.read_release()

    age_sec = round(time.time() - refresh_ts) if refresh_ts else None
    stale = (age_sec is not None and age_sec > REFRESH_INTERVAL * 2)

    db_ok = False
    open_positions = 0
    try:
        open_positions = len(storage.get_open_positions())
        db_ok = True
    except Exception:
        pass

    status = "ok" if (ready and db_ok and not stale) else "degraded"
    code = 200 if status == "ok" else 503
    return Response(
        json.dumps({
            "status": status,
            "cache_ready": ready,
            "data_age_sec": age_sec,
            "stale": stale,
            "last_updated": ts,
            "db_ok": db_ok,
            "open_positions": open_positions,
            }),
        status=code,
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )

@app.route("/api/summary")
def api_summary():
    """Resumen diario en texto plano — consumible desde Telegram bot o scripts externos."""
    cache_lock.read_acquire()
    try:
        market_data = cache.get("data") or {}
        last_updated = cache.get("last_updated", "—")
    finally:
        cache_lock.read_release()

    try:
        capital   = storage.get_capital()
        positions = storage.get_open_positions()
        vix       = indicators.get_vix_sma5()
        regime    = indicators.get_spy_regime().get("regime", "—")
        tmap      = {r["ticker"]: r for rows in market_data.values() for r in rows}
        open_val  = sum(
            p["shares"] * tmap[p["ticker"]]["price"]
            if p["ticker"] in tmap else 0
            for p in positions
        )
        total_eq  = round(capital + open_val, 2)
        total_ret = round((total_eq - PAPER2_INITIAL_CAP) / PAPER2_INITIAL_CAP * 100, 2)

        lines = [
            f"Market Tracker — {last_updated}",
            f"Equity: {total_eq:.0f}€ ({'+' if total_ret>=0 else ''}{total_ret:.1f}%)",
            f"Capital libre: {capital:.0f}€  |  En posiciones: {open_val:.0f}€",
            f"VIX: {f'{vix:.1f}' if vix else '—'}  |  Régimen: {regime}",
            "",
        ]
        if positions:
            lines.append(f"Posiciones abiertas ({len(positions)}):")
            for p in positions:
                ret = p.get("ret_pct", 0)
                sign = "+" if ret >= 0 else ""
                lines.append(f"  {p['ticker']:8s} {sign}{ret:.1f}%  {p.get('hours_held',0):.0f}h  score:{p.get('score','—')}")
        else:
            lines.append("Sin posiciones abiertas.")

        # Top 5 señales
        all_rows = [r for rows in market_data.values() for r in rows]
        top5 = sorted(
            [r for r in all_rows if isinstance(r.get("inv_score"), (int, float))],
            key=lambda r: r["inv_score"], reverse=True
        )[:5]
        if top5:
            lines += ["", "Top señales:"]
            for r in top5:
                lines.append(f"  {r['ticker']:8s} score:{r['inv_score']}  RSI:{r.get('rsi','—')}  BB:{r.get('bb_pct','—')}%")

        return Response("\n".join(lines), mimetype="text/plain; charset=utf-8",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        return Response(f"Error: {e}", mimetype="text/plain"), 500


@app.route("/api/ping")
def api_ping():
    cache_lock.read_acquire()
    try:
        ready = cache.get("data") is not None
        ts = cache.get("last_updated")
        refresh_ts = cache.get("last_refresh_ts")
    finally:
        cache_lock.read_release()
    age = round(time.time() - refresh_ts) if refresh_ts else 0
    vix = indicators.get_vix_sma5()
    return Response(json.dumps({"ready": ready, "last_updated": ts, "data_age_sec": age, "vix_sma5": vix}),
                    mimetype="application/json", headers={"Cache-Control": "no-store"})

@app.route("/api/wake", methods=["GET"])
def api_wake():
    cache_lock.read_acquire()
    try:
        last = cache.get("last_updated")
    finally:
        cache_lock.read_release()
    if last:
        try:
            last_dt = datetime.strptime(last, "%d-%b-%y %H:%M")
            age_min = (datetime.now() - last_dt).total_seconds() / 60
            if age_min < 25:
                return jsonify({"status": "ok", "msg": f"datos frescos ({age_min:.0f} min)"})
        except: pass
    threading.Thread(target=refresh_data, daemon=True).start()
    return jsonify({"status": "refreshing"})

@app.route("/service-worker.js")
def service_worker():
    sw = os.path.join(os.path.dirname(__file__), "service-worker.js")
    if not os.path.exists(sw): return Response("//", mimetype="application/javascript"), 404
    with open(sw) as f: content = f.read()
    return Response(content, mimetype="application/javascript")

@app.route("/favicon.ico")
def favicon():
    return Response(b"", mimetype="image/x-icon")

# ── BOOT ──────────────────────────────────────────────────────────────────────
_bg_started = False
_bg_lock = threading.Lock()
def _start_background():
    global _bg_started
    with _bg_lock:
        if not _bg_started:
            _bg_started = True
            threading.Thread(target=background_refresh, daemon=True).start()
            log.info("background_refresh arrancado")

_start_background()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
