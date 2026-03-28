"""
paper_engine.py — Motor de paper trading Score85.

Fixes aplicados:
  - Equity log reconciliado en EUR (fix #5)
  - Trailing stop adaptado al ATR del ticker (fix #9)
  - Sin doble deducción de capital (fix #1)
  - Solo background_refresh ejecuta trading (fix #2)
"""
import os
import json
import threading
from datetime import datetime, timedelta

from config import (
    PAPER2_FILE, PAPER2_INITIAL_CAP, PAPER2_POSITION_PCT, PAPER2_MIN_SCORE,
    PAPER2_HOLD_HOURS, PAPER2_STOP_LOSS, PAPER2_TAKE_PROFIT,
    PAPER2_TRAILING_MIN, PAPER2_MAX_HOURS,
    ALPACA_TRADEABLE, EU_TICKERS, FUTURES_TICKERS,
)
from indicators import get_eurusd, get_trailing_pct
import alpaca_api

paper2_lock = threading.Lock()
_paper2_mem: dict | None = None


def load():
    global _paper2_mem
    if _paper2_mem is not None:
        return _paper2_mem
    if os.path.exists(PAPER2_FILE):
        try:
            with open(PAPER2_FILE) as f:
                _paper2_mem = json.load(f)
            print(f"[paper2] cargado: {len(_paper2_mem.get('closed',[]))} trades cerrados")
            return _paper2_mem
        except Exception as e:
            print(f"[paper2] error leyendo JSON: {e}")
    _paper2_mem = {
        "capital": PAPER2_INITIAL_CAP, "open": [], "closed": [],
        "equity_log": [], "cooldowns": {},
    }
    return _paper2_mem


def save(data):
    global _paper2_mem
    _paper2_mem = data
    tmp = PAPER2_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, PAPER2_FILE)
    except Exception as e:
        print(f"[paper2] error guardando: {e}")


def reset():
    global _paper2_mem
    _paper2_mem = None
    if os.path.exists(PAPER2_FILE):
        os.remove(PAPER2_FILE)


def is_market_open(ticker):
    """Per-ticker market hours check."""
    now_utc = datetime.utcnow()
    if now_utc.weekday() >= 5:
        return False
    t = now_utc.hour * 60 + now_utc.minute
    if ticker in FUTURES_TICKERS:
        return True
    if ticker in EU_TICKERS:
        return 8 * 60 <= t <= 16 * 60 + 30
    return 13 * 60 + 30 <= t <= 20 * 60


def _to_eur(price_native, currency):
    """Convierte un precio nativo (USD o EUR) a EUR para el portfolio."""
    fx = get_eurusd()
    if currency == "USD" and fx > 0:
        return price_native * fx  # USD × (EUR/USD) = EUR
    return price_native


def _to_native(eur_price, currency):
    """Convierte un precio EUR del row a moneda nativa."""
    fx = get_eurusd()
    if currency == "USD" and fx > 0:
        return round(eur_price / fx, 4)
    return eur_price


def run_trading(market_data):
    """Ejecuta la lógica de trading. SOLO llamar desde background_refresh."""
    with paper2_lock:
        pt = load()
        now_dt  = datetime.now()
        now_str = now_dt.strftime("%Y-%m-%d %H:%M")
        changed = False

        ticker_map = {}
        for group_rows in market_data.values():
            for row in group_rows:
                ticker_map[row["ticker"]] = row

        # Clean expired cooldowns
        if "cooldowns" not in pt:
            pt["cooldowns"] = {}
        expired = [t for t, until in pt["cooldowns"].items()
                   if datetime.strptime(until, "%Y-%m-%d %H:%M") <= now_dt]
        for t in expired:
            del pt["cooldowns"][t]

        # ── Evaluar posiciones abiertas ───────────────────────────────────
        still_open = []
        for pos in pt["open"]:
            ticker = pos["ticker"]
            row    = ticker_map.get(ticker)
            entry_dt   = datetime.strptime(pos["entry_date"], "%Y-%m-%d %H:%M")
            hours_held = (now_dt - entry_dt).total_seconds() / 3600

            # Precio en moneda nativa de la posición
            currency = pos.get("currency", "EUR")
            if row:
                current_price = _to_native(row["price"], currency)
            else:
                current_price = pos.get("current_price", pos["entry_price"])

            ret_pct       = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
            current_score = row.get("inv_score") if row else None

            # Actualizar peak
            peak_price = pos.get("peak_price", pos["entry_price"])
            if current_price > peak_price:
                peak_price = current_price
                pos["peak_price"] = round(peak_price, 4)
                changed = True

            trailing_drop = (current_price - peak_price) / peak_price * 100

            exit_reason    = None
            needs_cooldown = False

            # ── ATR trailing stop (adaptado a volatilidad del ticker) ─────
            trailing_pct = pos.get("trailing_pct", get_trailing_pct(ticker))
            if "trailing_pct" not in pos:
                pos["trailing_pct"] = trailing_pct
                changed = True

            # Take profit → activar trailing
            if not pos.get("trailing_active") and ret_pct >= PAPER2_TAKE_PROFIT:
                pos["trailing_active"] = True
                pos["peak_price"]      = round(current_price, 4)
                changed = True
                print(f"[paper2] {ticker} trailing activado +{ret_pct:.1f}% (ATR trailing: {trailing_pct}%)")

            # Stop loss duro
            if ret_pct <= PAPER2_STOP_LOSS:
                exit_reason    = f"Stop loss {ret_pct:.1f}%"
                needs_cooldown = True

            # Trailing stop activo (usando ATR trailing)
            elif pos.get("trailing_active") and trailing_drop <= -trailing_pct:
                exit_reason = f"Trailing stop {trailing_drop:.1f}% (ATR: {trailing_pct}%)"

            # Score baja con trailing activo
            elif pos.get("trailing_active") and current_score is not None and current_score < PAPER2_MIN_SCORE:
                exit_reason = f"Score bajó a {current_score} (< {PAPER2_MIN_SCORE})"

            # Fase 2: ≥24h
            elif hours_held >= PAPER2_HOLD_HOURS:
                if ret_pct >= 0:
                    if current_score is not None and current_score >= PAPER2_MIN_SCORE:
                        if not pos.get("trailing_active") and ret_pct >= PAPER2_TRAILING_MIN:
                            pos["trailing_active"] = True
                            pos["peak_price"]      = round(current_price, 4)
                            changed = True
                    else:
                        exit_reason = f"24h cumplidas, score {current_score} < {PAPER2_MIN_SCORE}"
                else:
                    if hours_held >= PAPER2_MAX_HOURS:
                        exit_reason    = f"48h con retorno negativo {ret_pct:.1f}%"
                        needs_cooldown = True

            # ── Ejecutar salida ───────────────────────────────────────────
            if exit_reason:
                # PnL en moneda nativa, capital en EUR
                pnl_native = pos["shares"] * (current_price - pos["entry_price"])
                eur_value  = pos["shares"] * _to_eur(current_price, currency)
                pt["capital"] += eur_value
                pt["closed"].append({
                    "ticker":          ticker,
                    "name":            pos["name"],
                    "entry_date":      pos["entry_date"],
                    "exit_date":       now_str,
                    "entry_price":     pos["entry_price"],
                    "exit_price":      round(current_price, 2),
                    "peak_price":      round(peak_price, 2),
                    "currency":        currency,
                    "shares":          pos["shares"],
                    "ret_pct":         round(ret_pct, 2),
                    "pnl":             round(pnl_native, 2),
                    "pnl_eur":         round(pos["shares"] * (_to_eur(current_price, currency) - _to_eur(pos["entry_price"], currency)), 2),
                    "reason":          exit_reason,
                    "entry_score":     pos.get("entry_score", "—"),
                    "hours_held":      round(hours_held, 1),
                    "trailing_active": pos.get("trailing_active", False),
                    "trailing_pct":    trailing_pct,
                })
                if needs_cooldown:
                    pt["cooldowns"][ticker] = (now_dt + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
                # Alpaca sell
                threading.Thread(target=alpaca_api.place_order, args=(ticker, "sell", 0), daemon=True).start()
                changed = True
            else:
                if row:
                    pos["current_price"]    = round(current_price, 2)
                    pos["ret_pct"]          = round(ret_pct, 2)
                    pos["hours_held"]       = round(hours_held, 1)
                    pos["hours_left"]       = round(max(0, PAPER2_HOLD_HOURS - hours_held), 1)
                    pos["score"]            = current_score
                    pos["trailing_active"]  = pos.get("trailing_active", False)
                    pos["peak_price"]       = round(peak_price, 4)
                    pos["trailing_drop"]    = round(trailing_drop, 2)
                    pos["trailing_pct"]     = trailing_pct
                    pos["waiting_recovery"] = (hours_held >= PAPER2_HOLD_HOURS and ret_pct < 0
                                               and hours_held < PAPER2_MAX_HOURS)
                still_open.append(pos)

        pt["open"] = still_open

        # ── Nuevas entradas ───────────────────────────────────────────────
        open_tickers = {p["ticker"] for p in pt["open"]}
        for ticker, row in ticker_map.items():
            if ticker in pt["cooldowns"]:
                continue
            score = row.get("inv_score")
            if score is None or score < PAPER2_MIN_SCORE:
                continue
            if ticker in open_tickers:
                continue
            if not is_market_open(ticker):
                continue
            position_size = pt["capital"] * PAPER2_POSITION_PCT
            if position_size < 1:
                continue

            # Precio nativo y trailing ATR
            fx = get_eurusd()
            if ticker in ALPACA_TRADEABLE and fx > 0:
                native_price = round(row["price"] / fx, 4)
                currency     = "USD"
            else:
                native_price = row["price"]
                currency     = "EUR"

            trailing_pct = get_trailing_pct(ticker)
            shares = position_size / row["price"]
            notional_usd = position_size / fx if fx > 0 else position_size
            pt["capital"] -= position_size
            pt["open"].append({
                "ticker":          ticker,
                "name":            row["name"],
                "entry_date":      now_str,
                "entry_price":     native_price,
                "current_price":   native_price,
                "peak_price":      native_price,
                "currency":        currency,
                "shares":          round(shares, 6),
                "ret_pct":         0.0,
                "trailing_drop":   0.0,
                "trailing_active": False,
                "trailing_pct":    trailing_pct,
                "hours_held":      0.0,
                "hours_left":      float(PAPER2_HOLD_HOURS),
                "entry_score":     score,
                "score":           score,
                "waiting_recovery": False,
            })
            open_tickers.add(ticker)
            if ticker in ALPACA_TRADEABLE and alpaca_api.alpaca_enabled():
                threading.Thread(target=alpaca_api.place_order, args=(ticker, "buy", notional_usd), daemon=True).start()
            changed = True

        # ── Equity log (reconciliado en EUR) ──────────────────────────────
        open_value_eur = 0
        for p in pt["open"]:
            row = ticker_map.get(p["ticker"])
            if row:
                # row["price"] ya está en EUR
                open_value_eur += p["shares"] * row["price"]
            else:
                # Fallback: convertir precio nativo a EUR
                open_value_eur += p["shares"] * _to_eur(p["entry_price"], p.get("currency", "EUR"))
        total_equity = round(pt["capital"] + open_value_eur, 2)
        pt["equity_log"].append({"date": now_str, "equity": total_equity})
        pt["equity_log"] = pt["equity_log"][-500:]

        save(pt)
        return pt, ticker_map


def sell_manual(ticker, market_data):
    """Venta manual desde el UI. Returns dict con status."""
    with paper2_lock:
        pt = load()
        ticker_map = {}
        for group_rows in market_data.values():
            for row in group_rows:
                ticker_map[row["ticker"]] = row

        pos = next((p for p in pt["open"] if p["ticker"] == ticker), None)
        if not pos:
            return {"error": "position not found"}

        row = ticker_map.get(ticker)
        now_dt  = datetime.now()
        now_str = now_dt.strftime("%Y-%m-%d %H:%M")

        currency = pos.get("currency", "EUR")
        if row:
            current_price = _to_native(row["price"], currency)
        else:
            current_price = pos["entry_price"]

        ret_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
        pnl     = pos["shares"] * (current_price - pos["entry_price"])

        # Capital en EUR
        eur_value = pos["shares"] * (row["price"] if row else _to_eur(pos["entry_price"], currency))
        pt["capital"] += eur_value
        pt["open"] = [p for p in pt["open"] if p["ticker"] != ticker]
        pt["closed"].append({
            "ticker":       ticker,
            "name":         pos["name"],
            "entry_date":   pos["entry_date"],
            "exit_date":    now_str,
            "entry_price":  pos["entry_price"],
            "exit_price":   round(current_price, 2),
            "peak_price":   round(pos.get("peak_price", pos["entry_price"]), 2),
            "currency":     currency,
            "shares":       pos["shares"],
            "ret_pct":      round(ret_pct, 2),
            "pnl":          round(pnl, 2),
            "reason":       "Venta manual",
            "entry_score":  pos.get("entry_score", "—"),
            "hours_held":   pos.get("hours_held", "—"),
            "trailing_active": pos.get("trailing_active", False),
        })
        if "cooldowns" not in pt:
            pt["cooldowns"] = {}
        pt["cooldowns"][ticker] = (now_dt + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
        save(pt)
        threading.Thread(target=alpaca_api.place_order, args=(ticker, "sell", 0), daemon=True).start()
    return {"status": "sold", "cooldown_hours": 48}


def get_read_only_state(market_data):
    """READ-ONLY: actualiza precios para display sin tomar decisiones."""
    ticker_map = {}
    for group_rows in market_data.values():
        for row in group_rows:
            ticker_map[row["ticker"]] = row

    with paper2_lock:
        pt = load()
        now_dt = datetime.now()
        for pos in pt["open"]:
            row = ticker_map.get(pos["ticker"])
            if not row:
                continue
            entry_dt   = datetime.strptime(pos["entry_date"], "%Y-%m-%d %H:%M")
            hours_held = (now_dt - entry_dt).total_seconds() / 3600
            currency = pos.get("currency", "EUR")
            current_price = _to_native(row["price"], currency)
            ret_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
            peak_price = pos.get("peak_price", pos["entry_price"])
            trailing_drop = (current_price - peak_price) / peak_price * 100 if peak_price > 0 else 0

            pos["current_price"]    = round(current_price, 2)
            pos["ret_pct"]          = round(ret_pct, 2)
            pos["hours_held"]       = round(hours_held, 1)
            pos["hours_left"]       = round(max(0, PAPER2_HOLD_HOURS - hours_held), 1)
            pos["score"]            = row.get("inv_score")
            pos["trailing_drop"]    = round(trailing_drop, 2)
            pos["waiting_recovery"] = (hours_held >= PAPER2_HOLD_HOURS and ret_pct < 0 and hours_held < PAPER2_MAX_HOURS)

    # Equity en EUR
    open_value = sum(
        p["shares"] * ticker_map.get(p["ticker"], {}).get("price", _to_eur(p["entry_price"], p.get("currency", "EUR")))
        for p in pt["open"]
    )
    total_equity = round(pt["capital"] + open_value, 2)
    total_ret    = round((total_equity - PAPER2_INITIAL_CAP) / PAPER2_INITIAL_CAP * 100, 2)

    return {
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
    }
