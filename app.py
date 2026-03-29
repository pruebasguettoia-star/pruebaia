"""
app.py — Market Tracker (Flask).
Módulos: config, auth, alpaca_api, indicators, paper_engine, storage
"""
import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"]      = "1"
os.environ["OMP_NUM_THREADS"]      = "1"
os.environ["NUMEXPR_NUM_THREADS"]  = "1"

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
    EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO, EMAIL_ENABLED, alpaca_enabled, alpaca_url,
)
from auth import require_admin
import indicators
import paper_engine
import alpaca_api
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
    print("[boot] gzip activo")
except ImportError:
    pass

if alpaca_enabled():
    print(f"[boot] Alpaca activado — {alpaca_url()}")

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

# ── EMAIL with retry ─────────────────────────────────────────────────────────
_email_failures = 0

def send_alert_email(strong_buys, retry=2):
    global _email_failures
    if not EMAIL_ENABLED or not strong_buys:
        return
    for attempt in range(retry + 1):
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            subject = f"★ Market Tracker — {len(strong_buys)} Compra Fuerte"
            rows = "".join([
                f"<tr><td style='padding:6px 12px;font-weight:600'>{r['name']}</td>"
                f"<td style='padding:6px 12px;color:#aaa'>{r['ticker']}</td>"
                f"<td style='padding:6px 12px'>RSI {r['rsi']}</td>"
                f"<td style='padding:6px 12px'>BB {r['bb_pct']}%</td></tr>"
                for r in strong_buys
            ])
            html = f"""
            <div style='font-family:monospace;background:#0d0f12;color:#c8cdd6;padding:24px;border-radius:8px'>
              <h2 style='color:#34b566'>★ COMPRA FUERTE — {datetime.now().strftime('%d %b %Y %H:%M')}</h2>
              <table style='border-collapse:collapse;width:100%'>
                <tr style='color:#555e6e;font-size:11px'>
                  <th style='padding:6px 12px;text-align:left'>Nombre</th>
                  <th style='padding:6px 12px;text-align:left'>Ticker</th>
                  <th style='padding:6px 12px;text-align:left'>RSI</th>
                  <th style='padding:6px 12px;text-align:left'>BB%</th>
                </tr>{rows}</table></div>"""
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = EMAIL_FROM
            msg["To"] = EMAIL_TO
            msg.attach(MIMEText(html, "html"))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
                s.login(EMAIL_FROM, EMAIL_PASSWORD)
                s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
            print(f"[ALERT] Email OK: {len(strong_buys)} strong buy(s)")
            _email_failures = 0
            return
        except Exception as e:
            _email_failures += 1
            if attempt < retry:
                print(f"[ALERT] Email intento {attempt+1} falló: {e} — reintentando en 10s")
                time.sleep(10)
            else:
                print(f"[ALERT] Email falló tras {retry+1} intentos: {e}")

# ── DATA REFRESH ──────────────────────────────────────────────────────────────
def refresh_data():
    t0 = datetime.now()
    n  = sum(len(v) for v in GROUPS.values())
    print(f"[{t0.strftime('%H:%M:%S')}] Fetching {n} tickers…")

    try:
        _fx = yf.Ticker("EURUSD=X").fast_info
        _rate = getattr(_fx, "last_price", None)
        if _rate and _rate > 0:
            indicators.set_eurusd(round(1 / _rate, 6))
    except Exception as e:
        print(f"[fx] error: {e}")

    all_tasks = [(g, name, ticker) for g, tickers in GROUPS.items() for name, ticker in tickers]
    row_map = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(indicators.fetch_ticker, name, ticker): (g, ticker) for g, name, ticker in all_tasks}
        for fut in as_completed(futures):
            g, ticker = futures[fut]
            try:
                row = fut.result()
                if row: row_map[ticker] = (g, row)
            except Exception as e:
                print(f"  [warn] {ticker}: {e}")

    result = {g: [] for g in GROUPS}
    strong_buys = []
    for g, name, ticker in all_tasks:
        if ticker in row_map:
            _, row = row_map[ticker]
            result[g].append(row)
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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Done in {elapsed:.1f}s.")
    indicators.purge_intraday_cache()
    if strong_buys:
        threading.Thread(target=send_alert_email, args=(strong_buys,), daemon=True).start()
        threading.Thread(target=telegram_alerts.alert_strong_buys, args=(strong_buys,), daemon=True).start()


def background_refresh():
    # Init paper engine (migra JSON → SQLite si existe)
    paper_engine.init()
    while True:
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
                print("[bg] paper2 actualizado")
            except Exception as e:
                print(f"[bg] paper2 error: {e}")
        gc.collect()
        elapsed = time.monotonic() - t_start
        time.sleep(max(0, REFRESH_INTERVAL - elapsed))


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
            bd = r.get("inv_score_breakdown")
            if isinstance(bd, dict):
                r["inv_score_breakdown"] = indicators.breakdown_to_str(bd)
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
        "email_failures": _email_failures,
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
@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    t = threading.Thread(target=refresh_data, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})

# ── PROTECTED ─────────────────────────────────────────────────────────────────
@app.route("/api/config/email", methods=["POST"])
@require_admin
def config_email():
    return jsonify({"enabled": EMAIL_ENABLED})

@app.route("/api/config/email", methods=["GET"])
@require_admin
def get_email_config():
    return jsonify({"enabled": EMAIL_ENABLED, "to": EMAIL_TO, "from": EMAIL_FROM})

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
    cached = indicators.chart_cache_get(ticker)
    if cached: return jsonify(cached)
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="2y", auto_adjust=True)
        if hist.empty: return jsonify({"error": "no data"}), 404
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
        close = hist["Close"]
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi_s = 100 - (100 / (1 + rs))
        sma20 = close.rolling(20).mean(); std20 = close.rolling(20).std()
        bb_up = sma20 + 2 * std20; bb_lo = sma20 - 2 * std20
        sma50 = close.rolling(50).mean(); sma200 = close.rolling(200).mean()
        signals = []
        for i in range(len(hist)):
            r = float(rsi_s.iloc[i]) if not rsi_s.isna().iloc[i] else None
            bb = None
            if not bb_up.isna().iloc[i] and not bb_lo.isna().iloc[i]:
                rng = float(bb_up.iloc[i]) - float(bb_lo.iloc[i])
                if rng > 0: bb = (float(close.iloc[i]) - float(bb_lo.iloc[i])) / rng * 100
            s50 = float(sma50.iloc[i]) if not sma50.isna().iloc[i] else None
            s200 = float(sma200.iloc[i]) if not sma200.isna().iloc[i] else None
            if r and bb and s50 and s200 and r <= 30 and bb <= 20 and s50 > s200:
                signals.append({"time": int(hist.index[i].timestamp()), "price": round(float(close.iloc[i]), 4)})
        def series(s):
            return [{"time": int(i.timestamp()), "value": round(float(v), 4)} for i, v in s.items() if v == v]
        candles = []
        for i in range(len(hist)):
            row = hist.iloc[i]
            vol = None
            if "Volume" in hist.columns:
                v = row["Volume"]
                if v == v and v > 0: vol = int(v)
            candles.append({"time": int(hist.index[i].timestamp()), "open": round(float(row["Open"]), 4),
                "high": round(float(row["High"]), 4), "low": round(float(row["Low"]), 4),
                "close": round(float(row["Close"]), 4), "volume": vol})
        result = {"ticker": ticker, "candles": candles, "rsi": series(rsi_s),
            "bb_up": series(bb_up), "bb_lo": series(bb_lo),
            "sma50": series(sma50), "sma200": series(sma200), "signals": signals}
        indicators.chart_cache_set(ticker, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/dist/<ticker>")
def api_dist(ticker):
    cached = indicators.dist_cache_get(ticker)
    if cached: return jsonify(cached)
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="10y", auto_adjust=True)
        if hist.empty or len(hist) < 60: return jsonify({"error": "insufficient data"}), 404
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
        close = hist["Close"]
        rets = sorted(round((float(close.iloc[i+30]) - float(close.iloc[i])) / float(close.iloc[i]) * 100, 2)
                       for i in range(len(close) - 30) if math.isfinite((float(close.iloc[i+30]) - float(close.iloc[i])) / float(close.iloc[i]) * 100))
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
            b = yf.Ticker(ticker).info.get("beta")
            if b and math.isfinite(float(b)): beta = round(float(b), 2)
        except: pass
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
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── UTILITY ───────────────────────────────────────────────────────────────────
@app.route("/api/ping")
def api_ping():
    cache_lock.read_acquire()
    try:
        ready = cache.get("data") is not None
        ts = cache.get("last_updated")
        refresh_ts = cache.get("last_refresh_ts")
    finally:
        cache_lock.read_release()
    age = round(time.time() - refresh_ts) if refresh_ts else None
    return Response(json.dumps({"ready": ready, "last_updated": ts, "data_age_sec": age}),
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
def _start_background():
    global _bg_started
    if not _bg_started:
        _bg_started = True
        threading.Thread(target=background_refresh, daemon=True).start()
        print("[boot] background_refresh arrancado")

_start_background()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
