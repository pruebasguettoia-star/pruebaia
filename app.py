"""
app.py — Market Tracker (Flask). Thin routing layer.

Módulos:
  config.py        → constantes, tickers, settings
  auth.py          → autenticación por token
  alpaca_api.py    → integración Alpaca
  indicators.py    → data fetch, indicadores, scoring, ATR
  paper_engine.py  → lógica de paper trading
"""
import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"]      = "1"
os.environ["OMP_NUM_THREADS"]      = "1"
os.environ["NUMEXPR_NUM_THREADS"]  = "1"

from flask import Flask, render_template, jsonify, request
import yfinance as yf
import threading
import math
import time
import gc
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    GROUPS, USD_TICKERS, ALPACA_TRADEABLE, REFRESH_INTERVAL,
    CHART_TTL, CHART_MAX, PAPER2_INITIAL_CAP, PAPER2_HOLD_HOURS, PAPER2_MIN_SCORE,
    EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO, EMAIL_ENABLED,
    alpaca_enabled, alpaca_url,
)
from auth import require_admin
import indicators
import paper_engine
import alpaca_api

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
    print("[boot] flask-compress no instalado")

if alpaca_enabled():
    print(f"[boot] Alpaca activado — {alpaca_url()}")
else:
    print("[boot] Alpaca desactivado")

# ── GLOBAL STATE ──────────────────────────────────────────────────────────────
cache = {"data": None, "last_updated": None}
lock  = threading.Lock()
alerted = set()

# ── EMAIL ALERTS ──────────────────────────────────────────────────────────────
def send_alert_email(strong_buys):
    if not EMAIL_ENABLED or not strong_buys:
        return
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
        </div>"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_FROM, EMAIL_PASSWORD)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"[ALERT] Email: {len(strong_buys)} strong buy(s)")
    except Exception as e:
        print(f"[ALERT] Email failed: {e}")

# ── DATA REFRESH ──────────────────────────────────────────────────────────────
def refresh_data():
    t0 = datetime.now()
    n  = sum(len(v) for v in GROUPS.values())
    print(f"[{t0.strftime('%H:%M:%S')}] Fetching {n} tickers…")

    # EUR/USD
    try:
        _fx = yf.Ticker("EURUSD=X").fast_info
        _rate = getattr(_fx, "last_price", None)
        if _rate and _rate > 0:
            indicators.set_eurusd(round(1 / _rate, 6))
    except Exception as e:
        print(f"[fx] error: {e}")

    all_tasks = [(group, name, ticker) for group, tickers in GROUPS.items() for name, ticker in tickers]
    row_map = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(indicators.fetch_ticker, name, ticker): (group, ticker)
                   for group, name, ticker in all_tasks}
        for fut in as_completed(futures):
            group, ticker = futures[fut]
            try:
                row = fut.result()
                if row:
                    row_map[ticker] = (group, row)
            except Exception as e:
                print(f"  [warn] {ticker}: {e}")

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

    indicators.purge_intraday_cache()
    if strong_buys_this_cycle:
        threading.Thread(target=send_alert_email, args=(strong_buys_this_cycle,), daemon=True).start()


def background_refresh():
    while True:
        t_start = time.monotonic()
        refresh_data()
        with lock:
            market_data = cache.get("data") or {}
        if market_data:
            try:
                paper_engine.run_trading(market_data)
                print("[bg] paper2 actualizado")
            except Exception as e:
                print(f"[bg] paper2 error: {e}")
        gc.collect()
        elapsed = time.monotonic() - t_start
        time.sleep(max(0, REFRESH_INTERVAL - elapsed))

# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/paper2")
def paper2_page():
    return render_template("paper2.html")

@app.route("/api/data")
def api_data():
    with lock:
        data         = cache["data"]
        last_updated = cache["last_updated"]
    if not data:
        return jsonify({"error": "no data yet"}), 503
    import copy
    out = {}
    for group, rows in data.items():
        out[group] = []
        for row in rows:
            r = dict(row)
            bd = r.get("inv_score_breakdown")
            if isinstance(bd, dict):
                r["inv_score_breakdown"] = indicators.breakdown_to_str(bd)
            out[group].append(r)

    # Cooldowns info
    cooldowns = {}
    try:
        pt = paper_engine._paper2_mem
        if pt and pt.get("cooldowns"):
            now_dt = datetime.now()
            for ticker, until_str in pt["cooldowns"].items():
                until_dt = datetime.strptime(until_str, "%Y-%m-%d %H:%M")
                hours_left = (until_dt - now_dt).total_seconds() / 3600
                if hours_left > 0:
                    cooldowns[ticker] = round(hours_left, 1)
    except Exception:
        pass

    resp = jsonify({"data": out, "last_updated": last_updated, "cooldowns": cooldowns})
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/api/paper2")
def api_paper2():
    """READ-ONLY — solo actualiza display, no toma decisiones."""
    with lock:
        market_data = cache.get("data") or {}
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
    with lock:
        market_data = cache.get("data") or {}
    result = paper_engine.sell_manual(ticker, market_data)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    t = threading.Thread(target=refresh_data, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})

# ── PROTECTED ENDPOINTS ───────────────────────────────────────────────────────
@app.route("/api/config/email", methods=["POST"])
@require_admin
def config_email():
    d = request.json
    # Solo actualizar en memoria — las credenciales reales vienen de env vars
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
        pt = paper_engine._paper2_mem
        if pt is not None:
            data = json.dumps(pt, indent=2)
        else:
            data = "{}"
        zf.writestr("paper2_trades.json", data)
    buf.seek(0)
    from flask import Response
    return Response(buf.read(), mimetype="application/zip",
                    headers={"Content-Disposition": "attachment; filename=paper_backup.zip"})

# ── ALPACA ENDPOINTS ──────────────────────────────────────────────────────────
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
        "currency": acct.get("currency"),
    })

@app.route("/api/alpaca/positions")
def api_alpaca_positions():
    if not alpaca_enabled():
        return jsonify({"enabled": False})
    positions = alpaca_api.get_positions()
    if positions is None:
        return jsonify({"enabled": True, "error": "failed"})
    return jsonify({"enabled": True, "positions": positions})

# ── CHART / DIST ──────────────────────────────────────────────────────────────
@app.route("/api/chart/<ticker>")
def api_chart(ticker):
    cached = indicators.chart_cache_get(ticker)
    if cached:
        return jsonify(cached)
    try:
        t    = yf.Ticker(ticker)
        hist = t.history(period="2y", auto_adjust=True)
        if hist.empty:
            return jsonify({"error": "no data"}), 404
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
        close = hist["Close"]

        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi_s = 100 - (100 / (1 + rs))
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        bb_up = sma20 + 2 * std20
        bb_lo = sma20 - 2 * std20
        sma50  = close.rolling(50).mean()
        sma200 = close.rolling(200).mean()

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
                    signals.append({"time": int(hist.index[i].timestamp()), "price": round(float(close.iloc[i]), 4)})

        def series(s):
            return [{"time": int(i.timestamp()), "value": round(float(v), 4)}
                    for i, v in s.items() if v == v]

        candles = []
        for i in range(len(hist)):
            row = hist.iloc[i]
            vol = None
            if "Volume" in hist.columns:
                v = row["Volume"]
                if v == v and v > 0: vol = int(v)
            candles.append({
                "time": int(hist.index[i].timestamp()),
                "open": round(float(row["Open"]), 4), "high": round(float(row["High"]), 4),
                "low": round(float(row["Low"]), 4), "close": round(float(row["Close"]), 4),
                "volume": vol,
            })

        result = {
            "ticker": ticker, "candles": candles, "rsi": series(rsi_s),
            "bb_up": series(bb_up), "bb_lo": series(bb_lo),
            "sma50": series(sma50), "sma200": series(sma200), "signals": signals,
        }
        indicators.chart_cache_set(ticker, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/dist/<ticker>")
def api_dist(ticker):
    cached = indicators.dist_cache_get(ticker)
    if cached:
        return jsonify(cached)
    try:
        t    = yf.Ticker(ticker)
        hist = t.history(period="10y", auto_adjust=True)
        if hist.empty or len(hist) < 60:
            return jsonify({"error": "insufficient data"}), 404
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
        close = hist["Close"]

        rets_30 = []
        for i in range(len(close) - 30):
            r = (float(close.iloc[i + 30]) - float(close.iloc[i])) / float(close.iloc[i]) * 100
            if math.isfinite(r): rets_30.append(round(r, 2))
        if not rets_30:
            return jsonify({"error": "no returns"}), 404
        rets_30.sort()
        n = len(rets_30)

        prob_up = round(sum(1 for r in rets_30 if r > 0) / n * 100, 1)
        median  = round(rets_30[n // 2], 2)
        mean    = round(sum(rets_30) / n, 2)
        var_95  = round(rets_30[int(n * 0.05)], 2)
        var_99  = round(rets_30[int(n * 0.01)], 2)

        bucket_w  = 2.0; buck_min = -30.0; buck_max = 30.0
        n_buckets = int((buck_max - buck_min) / bucket_w)
        buckets   = [0] * n_buckets
        for r in rets_30:
            idx = int((r - buck_min) / bucket_w)
            idx = max(0, min(n_buckets - 1, idx))
            buckets[idx] += 1
        bucket_pct    = [round(b / n * 100, 2) for b in buckets]
        bucket_labels = [round(buck_min + i * bucket_w, 0) for i in range(n_buckets)]

        monthly = {}
        for i in range(len(close) - 21):
            month = hist.index[i].month
            r = (float(close.iloc[i + 21]) - float(close.iloc[i])) / float(close.iloc[i]) * 100
            if math.isfinite(r): monthly.setdefault(month, []).append(r)
        seasonality = {}
        for m in range(1, 13):
            vals = monthly.get(m, [])
            if vals:
                seasonality[m] = {"mean": round(sum(vals)/len(vals), 2), "prob_up": round(sum(1 for v in vals if v > 0)/len(vals)*100, 1), "n": len(vals)}
            else:
                seasonality[m] = {"mean": None, "prob_up": None, "n": 0}

        beta_dist = None
        try:
            b = yf.Ticker(ticker).info.get("beta")
            if b is not None and math.isfinite(float(b)):
                beta_dist = round(float(b), 2)
        except Exception:
            pass

        result = {
            "ticker": ticker, "n_samples": n, "years": round(n / 252, 1),
            "prob_up": prob_up, "prob_down": round(100 - prob_up, 1),
            "median": median, "mean": mean, "var_95": var_95, "var_99": var_99,
            "best_30": round(rets_30[-1], 2), "worst_30": round(rets_30[0], 2),
            "buckets": bucket_pct, "bucket_labels": bucket_labels,
            "seasonality": seasonality, "beta": beta_dist,
        }
        indicators.dist_cache_set(ticker, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── UTILITY ENDPOINTS ─────────────────────────────────────────────────────────
@app.route("/api/ping")
def api_ping():
    with lock:
        ready = cache.get("data") is not None
        ts    = cache.get("last_updated")
    from flask import Response
    return Response(json.dumps({"ready": ready, "last_updated": ts}),
                    mimetype="application/json", headers={"Cache-Control": "no-store"})

@app.route("/api/wake", methods=["GET"])
def api_wake():
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

@app.route("/service-worker.js")
def service_worker():
    from flask import Response
    sw_path = os.path.join(os.path.dirname(__file__), "service-worker.js")
    if not os.path.exists(sw_path):
        return Response("// not found", mimetype="application/javascript"), 404
    with open(sw_path) as f:
        content = f.read()
    return Response(content, mimetype="application/javascript")

@app.route("/favicon.ico")
def favicon():
    from flask import Response
    return Response(b"", mimetype="image/x-icon")

# ── BOOT ──────────────────────────────────────────────────────────────────────
_bg_started = False
def _start_background():
    global _bg_started
    if not _bg_started:
        _bg_started = True
        t = threading.Thread(target=background_refresh, daemon=True)
        t.start()
        print("[boot] background_refresh arrancado")

_start_background()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
