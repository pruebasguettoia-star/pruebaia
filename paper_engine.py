"""
paper_engine.py — Motor de paper trading Score85.
Usa SQLite (storage.py) en vez de JSON.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from config import (
    PAPER2_FILE, PAPER2_INITIAL_CAP, PAPER2_POSITION_PCT, PAPER2_MIN_SCORE,
    PAPER2_HOLD_HOURS, PAPER2_STOP_LOSS, PAPER2_TAKE_PROFIT,
    PAPER2_TRAILING_MIN, PAPER2_MAX_HOURS,
    MOM_HOLD_HOURS, MOM_MAX_HOURS, MOM_TAKE_PROFIT, MOM_TRAILING_MIN,
    ALPACA_TRADEABLE, EU_TICKERS, FUTURES_TICKERS,
)
from indicators import get_eurusd, get_trailing_pct, get_spy_regime, is_momentum_candidate
import alpaca_api
import storage
import telegram_alerts

paper2_lock = threading.Lock()

# Pools compartidos — evitan acumulación de threads daemon en ciclos con muchas operaciones
_tg_pool     = ThreadPoolExecutor(max_workers=3, thread_name_prefix="tg")
_alpaca_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="alpaca")

def is_market_open(ticker):
    now_utc = datetime.utcnow()
    if now_utc.weekday() >= 5:
        return False
    t = now_utc.hour * 60 + now_utc.minute
    if ticker in FUTURES_TICKERS:
        return True
    if ticker in EU_TICKERS:
        # Europa: LSE/Euronext abren 08:00 hora local
        # En invierno (CET=UTC+1): 07:00–16:30 UTC
        # En verano (CEST=UTC+2):  06:00–15:30 UTC
        # Aproximación conservadora que cubre ambos: 06:00–16:30 UTC
        return 6 * 60 <= t <= 16 * 60 + 30
    # US: NYSE/NASDAQ 09:30–16:00 ET
    # ET invierno = UTC-5 → 14:30–21:00 UTC
    # ET verano  = UTC-4 → 13:30–20:00 UTC
    # Aproximación conservadora: 13:30–21:00 UTC
    return 13 * 60 + 30 <= t <= 21 * 60

def _to_eur(price_native, currency):
    fx = get_eurusd()
    return price_native * fx if currency == "USD" and fx > 0 else price_native

def _to_native(eur_price, currency):
    fx = get_eurusd()
    return round(eur_price / fx, 4) if currency == "USD" and fx > 0 else eur_price

def init():
    storage.init_db()
    open_pos = storage.get_open_positions()
    closed = storage.get_closed_trades(1)
    if not open_pos and not closed:
        storage.migrate_from_json(PAPER2_FILE)

def run_trading(market_data):
    with paper2_lock:
        now_dt = datetime.now()
        now_str = now_dt.strftime("%Y-%m-%d %H:%M")
        ticker_map = {}
        for group_rows in market_data.values():
            for row in group_rows:
                ticker_map[row["ticker"]] = row

        storage.clean_expired_cooldowns()
        cooldowns = storage.get_cooldowns()
        open_positions = storage.get_open_positions()

        for pos in open_positions:
            ticker = pos["ticker"]
            row = ticker_map.get(ticker)
            entry_dt = datetime.strptime(pos["entry_date"], "%Y-%m-%d %H:%M")
            hours_held = (now_dt - entry_dt).total_seconds() / 3600
            currency = pos.get("currency", "EUR")
            current_price = _to_native(row["price"], currency) if row else pos.get("current_price", pos["entry_price"])
            ret_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
            current_score = row.get("inv_score") if row else None
            peak_price = pos.get("peak_price", pos["entry_price"])
            if current_price > peak_price: peak_price = current_price
            trailing_drop = (current_price - peak_price) / peak_price * 100
            trailing_pct = pos.get("trailing_pct") or get_trailing_pct(ticker)
            exit_reason = None
            needs_cooldown = False
            trailing_active = bool(pos.get("trailing_active", False))

            # ── Parámetros según modo ─────────────────────────────────────────
            is_mom_trade = pos.get("trade_mode") == "momentum"
            hold_hours   = MOM_HOLD_HOURS  if is_mom_trade else PAPER2_HOLD_HOURS
            max_hours    = MOM_MAX_HOURS   if is_mom_trade else PAPER2_MAX_HOURS
            take_profit  = MOM_TAKE_PROFIT if is_mom_trade else PAPER2_TAKE_PROFIT
            trailing_min = MOM_TRAILING_MIN if is_mom_trade else PAPER2_TRAILING_MIN

            if not trailing_active and ret_pct > take_profit:
                trailing_active = True; peak_price = current_price
            if ret_pct <= PAPER2_STOP_LOSS:
                exit_reason = f"Stop loss {ret_pct:.1f}%"; needs_cooldown = True
            elif trailing_active and trailing_drop <= -trailing_pct:
                exit_reason = f"Trailing stop {trailing_drop:.1f}% (ATR:{trailing_pct}%)"
            elif trailing_active and current_score is not None and current_score < PAPER2_MIN_SCORE:
                exit_reason = f"Score bajó a {current_score}"
            elif hours_held >= hold_hours:
                if ret_pct >= 0:
                    if current_score is not None and current_score >= PAPER2_MIN_SCORE:
                        if not trailing_active and ret_pct >= trailing_min:
                            trailing_active = True; peak_price = current_price
                    else:
                        exit_reason = f"{hold_hours:.0f}h cumplidas, score {current_score} < {PAPER2_MIN_SCORE}"
                elif hours_held >= max_hours:
                    exit_reason = f"{max_hours:.0f}h negativo {ret_pct:.1f}%"; needs_cooldown = True

            if exit_reason:
                eur_value = pos["shares"] * _to_eur(current_price, currency)
                pnl_eur = pos["shares"] * (_to_eur(current_price, currency) - _to_eur(pos["entry_price"], currency))
                storage.add_capital(eur_value)
                storage.add_closed_trade({
                    "ticker": ticker, "name": pos.get("name"),
                    "entry_date": pos["entry_date"], "exit_date": now_str,
                    "entry_price": pos["entry_price"], "exit_price": round(current_price, 2),
                    "peak_price": round(peak_price, 2), "currency": currency,
                    "shares": pos["shares"], "ret_pct": round(ret_pct, 2),
                    "pnl": round(pnl_eur, 2),        # siempre en EUR — evita mezcla de divisas en stats
                    "pnl_eur": round(pnl_eur, 2), "reason": exit_reason,
                    "entry_score": pos.get("entry_score"), "hours_held": round(hours_held, 1),
                    "trailing_active": trailing_active, "trailing_pct": trailing_pct,
                })
                storage.remove_open_position(ticker)
                if needs_cooldown:
                    storage.set_cooldown(ticker, (now_dt + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M"))
                _alpaca_pool.submit(alpaca_api.place_order, ticker, "sell", 0)
                _tg_pool.submit(telegram_alerts.alert_trade_close, ticker, pos.get("name",""), round(ret_pct,2), round(pnl_eur,2), exit_reason, "EUR")
            else:
                storage.update_open_position(ticker, {
                    "current_price": round(current_price, 2), "peak_price": round(peak_price, 4),
                    "ret_pct": round(ret_pct, 2), "hours_held": round(hours_held, 1),
                    "hours_left": round(max(0, hold_hours - hours_held), 1),
                    "score": current_score, "trailing_active": int(trailing_active),
                    "trailing_pct": trailing_pct, "trailing_drop": round(trailing_drop, 2),
                    "waiting_recovery": int(hours_held >= hold_hours and ret_pct < 0 and hours_held < max_hours),
                })

        # ── Régimen de mercado ─────────────────────────────────────────────────
        regime_info = get_spy_regime()
        regime      = regime_info.get("regime", "dip_buying")  # "momentum" | "dip_buying"

        # Nuevas entradas — modo dual
        cooldowns    = storage.get_cooldowns()
        open_tickers = storage.get_open_tickers()
        capital      = storage.get_capital()

        for ticker, row in ticker_map.items():
            if ticker in cooldowns or ticker in open_tickers: continue
            if not is_market_open(ticker): continue

            score = row.get("inv_score")

            # ── Modo DIP-BUYING (siempre activo) ──────────────────────────────
            # Criterio original: score ≥ 85 (comprar activos sobrevendidos)
            is_dip  = score is not None and score >= PAPER2_MIN_SCORE
            # ── Modo MOMENTUM (solo cuando SPY y URTH > SMA50) ───────────────
            # Criterio: activo en tendencia alcista con fuerza relativa positiva
            is_mom  = regime == "momentum" and is_momentum_candidate(row)

            if not is_dip and not is_mom:
                continue

            # Determinar etiqueta del modo (dip gana si ambos aplican,
            # ya que ya se habría abierto por score — evita duplicados)
            trade_mode = "dip_buying" if is_dip else "momentum"

            position_size = capital * PAPER2_POSITION_PCT
            if position_size < 1: continue

            fx = get_eurusd()
            if ticker in ALPACA_TRADEABLE and fx > 0:
                native_price = round(row["price"] / fx, 4); currency = "USD"
            else:
                native_price = row["price"]; currency = "EUR"

            # shares se calcula sobre el precio nativo para que el P&L
            # (shares × Δnative) sea consistente con la divisa de entry_price.
            # position_size está en EUR → convertimos al nativo antes de dividir.
            native_position_size = position_size / fx if (currency == "USD" and fx > 0) else position_size
            shares       = native_position_size / native_price if native_price > 0 else 0
            notional_usd = position_size / fx if fx > 0 else position_size
            trailing_pct_entry = get_trailing_pct(ticker)

            storage.atomic_open_position({
                "ticker": ticker, "name": row["name"], "entry_date": now_str,
                "entry_price": native_price, "current_price": native_price,
                "peak_price": native_price, "currency": currency,
                "shares": round(shares, 6), "ret_pct": 0.0, "trailing_drop": 0.0,
                "trailing_active": False, "trailing_pct": trailing_pct_entry,
                "hours_held": 0.0, "hours_left": float(PAPER2_HOLD_HOURS),
                "entry_score": score, "score": score, "waiting_recovery": False,
                "trade_mode": trade_mode,
            }, cost_eur=position_size)
            capital -= position_size
            open_tickers.add(ticker)
            if ticker in ALPACA_TRADEABLE and alpaca_api.alpaca_enabled():
                _alpaca_pool.submit(alpaca_api.place_order, ticker, "buy", notional_usd)
            _tg_pool.submit(
                telegram_alerts.alert_trade_open,
                ticker, row["name"], score, native_price, currency,
            )

        # Equity log
        positions = storage.get_open_positions()
        open_value_eur = sum(
            p["shares"] * (
                ticker_map[p["ticker"]]["price"]
                if p["ticker"] in ticker_map
                else _to_eur(p["entry_price"], p.get("currency", "EUR"))
            )
            for p in positions
        )
        storage.add_equity_snapshot(storage.get_capital() + open_value_eur)
        return positions, ticker_map


def sell_manual(ticker, market_data):
    with paper2_lock:
        ticker_map = {}
        for group_rows in market_data.values():
            for row in group_rows:
                ticker_map[row["ticker"]] = row
        positions = storage.get_open_positions()
        pos = next((p for p in positions if p["ticker"] == ticker), None)
        if not pos: return {"error": "position not found"}
        row = ticker_map.get(ticker)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        currency = pos.get("currency", "EUR")
        current_price = _to_native(row["price"], currency) if row else pos["entry_price"]
        ret_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
        eur_exit_price  = row["price"] if row else _to_eur(current_price, currency)
        eur_entry_price = _to_eur(pos["entry_price"], currency)
        pnl_eur = pos["shares"] * (eur_exit_price - eur_entry_price)
        storage.add_capital(pos["shares"] * eur_exit_price)
        storage.remove_open_position(ticker)
        storage.add_closed_trade({
            "ticker": ticker, "name": pos.get("name"),
            "entry_date": pos["entry_date"], "exit_date": now_str,
            "entry_price": pos["entry_price"], "exit_price": round(current_price, 2),
            "peak_price": round(pos.get("peak_price", pos["entry_price"]), 2),
            "currency": currency, "shares": pos["shares"],
            "ret_pct": round(ret_pct, 2),
            "pnl": round(pnl_eur, 2),      # siempre en EUR — evita mezcla de divisas en stats
            "pnl_eur": round(pnl_eur, 2),
            "reason": "Venta manual", "entry_score": pos.get("entry_score"),
            "hours_held": pos.get("hours_held"), "trailing_active": pos.get("trailing_active", False),
            "trailing_pct": pos.get("trailing_pct"),
        })
        storage.set_cooldown(ticker, (datetime.now() + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M"))
        _alpaca_pool.submit(alpaca_api.place_order, ticker, "sell", 0)
        _tg_pool.submit(telegram_alerts.alert_trade_close, ticker, pos.get("name", ""), round(ret_pct, 2), round(pnl_eur, 2), "Venta manual", "EUR")
    return {"status": "sold", "cooldown_hours": 48}

def reset():
    storage.reset_all()

def get_read_only_state(market_data):
    ticker_map = {}
    for group_rows in market_data.values():
        for row in group_rows:
            ticker_map[row["ticker"]] = row
    positions = storage.get_open_positions()
    now_dt = datetime.now()
    for pos in positions:
        row = ticker_map.get(pos["ticker"])
        if not row: continue
        entry_dt = datetime.strptime(pos["entry_date"], "%Y-%m-%d %H:%M")
        hours_held = (now_dt - entry_dt).total_seconds() / 3600
        currency = pos.get("currency", "EUR")
        is_mom_ro = pos.get("trade_mode") == "momentum"
        hold_hrs_ro = MOM_HOLD_HOURS if is_mom_ro else PAPER2_HOLD_HOURS
        max_hrs_ro  = MOM_MAX_HOURS  if is_mom_ro else PAPER2_MAX_HOURS
        current_price = _to_native(row["price"], currency)
        ret_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
        peak_price = pos.get("peak_price", pos["entry_price"])
        # Actualizar peak en la vista (no persiste, solo para mostrar trailing correcto)
        if current_price > peak_price:
            peak_price = current_price
        trailing_drop = (current_price - peak_price) / peak_price * 100 if peak_price > 0 else 0
        pos["current_price"] = round(current_price, 2)
        pos["ret_pct"] = round(ret_pct, 2)
        pos["hours_held"] = round(hours_held, 1)
        pos["hours_left"] = round(max(0, hold_hrs_ro - hours_held), 1)
        pos["score"] = row.get("inv_score")
        pos["trailing_drop"] = round(trailing_drop, 2)
        pos["waiting_recovery"] = (hours_held >= hold_hrs_ro and ret_pct < 0 and hours_held < max_hrs_ro)

    open_value = sum(
        p["shares"] * (
            ticker_map[p["ticker"]]["price"]
            if p["ticker"] in ticker_map
            else _to_eur(p["entry_price"], p.get("currency", "EUR"))
        )
        for p in positions
    )
    capital = storage.get_capital()
    total_equity = round(capital + open_value, 2)
    total_ret = round((total_equity - PAPER2_INITIAL_CAP) / PAPER2_INITIAL_CAP * 100, 2)
    regime_info = get_spy_regime()
    return {
        "capital": round(capital, 2), "open_value": round(open_value, 2),
        "total_equity": total_equity, "total_ret": total_ret, "initial": PAPER2_INITIAL_CAP,
        "open": positions, "closed": storage.get_closed_trades(200),
        "equity_log": storage.get_equity_log(500),
        "min_score": PAPER2_MIN_SCORE, "hold_hours": PAPER2_HOLD_HOURS,
        "mom_hold_hours": MOM_HOLD_HOURS,
        "regime": regime_info,
    }
