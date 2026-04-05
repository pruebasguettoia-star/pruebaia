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
    TAKE_PROFIT_BY_REGIME, ATR_STOP_MULT, ATR_STOP_MIN, ATR_STOP_MAX,
    HOLD_EXT_SCORE, HOLD_EXT_RET_PCT, HOLD_EXT_MAX_HOURS,
    PYRA_ENABLED, PYRA_MIN_RET, PYRA_MIN_HOURS, PYRA_MIN_SCORE,
    PYRA_SIZE_PCT, PYRA_MAX_PER_POS,
    COOLDOWN_STOP_HOURS, COOLDOWN_TRAILING_HOURS,
    POS_VOL_LOW_ATR, POS_VOL_MED_ATR,
    POS_PCT_LOW_VOL, POS_PCT_MED_VOL, POS_PCT_HIGH_VOL,
    PARTIAL_SELL_ENABLED, PARTIAL_SELL_PCT,
    NON_TRADEABLE,
    POS_SIZE_ON_INITIAL_CAP,
    EARLY_EXIT_ENABLED, EARLY_EXIT_MIN_HOURS, EARLY_EXIT_MIN_RET, EARLY_EXIT_MAX_SCORE,
    COOLDOWN_EARLY_CHECK_HOURS, COOLDOWN_EARLY_MIN_SCORE,
    VIX_CIRCUIT_BREAKER, VIX_CIRCUIT_BREAKER_LEVEL,
    ATR_MAX_ENTRY,
    RSI_DIV_ENABLED, RSI_DIV_MIN_SCORE,
    DIV_YIELD_ENABLED, DIV_YIELD_MIN_PCT, DIV_MIN_SCORE,
    BONDS_ENABLED, BONDS_MIN_SCORE, BONDS_TICKERS,
)
from indicators import get_eurusd, get_trailing_pct, get_spy_regime, get_market_regime, get_vix_sma5, is_momentum_candidate, calc_atr_pct
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
    # Validación de config: PYRA_MIN_RET debe ser mayor que el take_profit mínimo
    # para evitar que pyramid y trailing se activen en el mismo ciclo
    min_take_profit = min(TAKE_PROFIT_BY_REGIME.values())
    if PYRA_MIN_RET >= min_take_profit:
        print(f"[config] AVISO: PYRA_MIN_RET({PYRA_MIN_RET}%) >= take_profit mínimo({min_take_profit}%) "
              f"— pyramid puede colisionar con trailing en {[k for k,v in TAKE_PROFIT_BY_REGIME.items() if v <= PYRA_MIN_RET]}")
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
            trailing_active = bool(pos.get("trailing_active", False))
            pyramid_count = int(pos.get("pyramid_count", 0))

            # ── Parámetros según modo ─────────────────────────────────────────
            is_mom_trade = pos.get("trade_mode") == "momentum"
            hold_hours   = MOM_HOLD_HOURS  if is_mom_trade else PAPER2_HOLD_HOURS
            trailing_min = MOM_TRAILING_MIN if is_mom_trade else PAPER2_TRAILING_MIN

            # ── ATR — una sola llamada, ya cacheado desde fetch_ticker ─────────
            atr_pct = calc_atr_pct(ticker)

            # ── Stop dinámico por ATR ─────────────────────────────────────────
            if atr_pct is not None:
                dynamic_stop = -round(max(ATR_STOP_MIN, min(ATR_STOP_MAX, ATR_STOP_MULT * atr_pct)), 2)
            else:
                dynamic_stop = PAPER2_STOP_LOSS

            # ── Trailing dinámico por régimen ─────────────────────────────────
            # BULL_QUIET activa el trailing más tarde para dejar correr tendencias
            if is_mom_trade:
                take_profit = MOM_TAKE_PROFIT
            else:
                scoring_regime = get_market_regime()
                take_profit = TAKE_PROFIT_BY_REGIME.get(scoring_regime, PAPER2_TAKE_PROFIT)

            # ── Hold máximo: extensión por score ─────────────────────────────
            # Si a las hold_hours el score es alto y hay beneficio, extender max
            if (current_score is not None and current_score >= HOLD_EXT_SCORE
                    and ret_pct >= HOLD_EXT_RET_PCT and not is_mom_trade):
                max_hours = HOLD_EXT_MAX_HOURS
            else:
                max_hours = MOM_MAX_HOURS if is_mom_trade else PAPER2_MAX_HOURS

            # ── Lógica de salida ──────────────────────────────────────────────
            if not trailing_active and ret_pct > take_profit:
                trailing_active = True
                peak_price = current_price
                # ── Salida parcial al activar trailing ────────────────────────
                # Vender PARTIAL_SELL_PCT de la posición, dejar el resto correr
                if PARTIAL_SELL_ENABLED and pos["shares"] > 0:
                    shares_to_sell  = pos["shares"] * PARTIAL_SELL_PCT
                    eur_partial     = shares_to_sell * _to_eur(current_price, currency)
                    pnl_partial_eur = shares_to_sell * (
                        _to_eur(current_price, currency) - _to_eur(pos["entry_price"], currency)
                    )
                    ret_partial = ret_pct   # mismo ret_pct en el momento de la parcial
                    storage.partial_close(ticker, shares_to_sell, eur_partial)
                    storage.add_closed_trade({
                        "ticker": ticker, "name": pos.get("name"),
                        "entry_date": pos["entry_date"], "exit_date": now_str,
                        "entry_price": pos["entry_price"],
                        "exit_price": round(current_price, 2),
                        "peak_price": round(peak_price, 2), "currency": currency,
                        "shares": round(shares_to_sell, 6),
                        "ret_pct": round(ret_partial, 2),
                        "pnl": round(pnl_partial_eur, 2),
                        "pnl_eur": round(pnl_partial_eur, 2),
                        "reason": f"Salida parcial {int(PARTIAL_SELL_PCT*100)}% al activar trailing",
                        "entry_score": pos.get("entry_score"),
                        "hours_held": round(hours_held, 1),
                        "trailing_active": True, "trailing_pct": trailing_pct,
                        "is_partial": True,
                    })
                    # Actualizar shares en la variable local para el resto del ciclo
                    pos = dict(pos)
                    pos["shares"] = pos["shares"] - shares_to_sell
                    _tg_pool.submit(
                        telegram_alerts.alert_trade_close,
                        ticker, pos.get("name", ""), round(ret_partial, 2),
                        round(pnl_partial_eur, 2),
                        f"Parcial {int(PARTIAL_SELL_PCT*100)}%", "EUR"
                    )
                    if ticker in ALPACA_TRADEABLE and alpaca_api.alpaca_enabled():
                        _alpaca_pool.submit(
                            alpaca_api.place_order, ticker, "sell", 0,
                            shares_to_sell   # qty para venta parcial
                        )
                    print(f"[partial] {ticker} vendido {int(PARTIAL_SELL_PCT*100)}% @ {current_price:.2f} ret={ret_pct:.1f}%")

            # Cooldown diferenciado: stop loss = fallo real (48h), trailing/score/tiempo = OK (24h)
            is_stop_loss_exit = False
            if ret_pct <= dynamic_stop:
                exit_reason = f"Stop loss {ret_pct:.1f}% (ATR stop {dynamic_stop:.1f}%)"
                is_stop_loss_exit = True
            elif trailing_active and trailing_drop <= -trailing_pct:
                exit_reason = f"Trailing stop {trailing_drop:.1f}% (ATR:{trailing_pct}%)"
            elif trailing_active and current_score is not None and current_score < PAPER2_MIN_SCORE:
                exit_reason = f"Score bajó a {current_score}"
            # ── Salida anticipada dinámica ────────────────────────────────────
            # Si el score cayó y ya hay beneficio suficiente, no esperar a hold_hours
            # Libera capital para nuevas señales mejores
            elif (EARLY_EXIT_ENABLED
                  and not trailing_active
                  and hours_held >= EARLY_EXIT_MIN_HOURS
                  and ret_pct >= EARLY_EXIT_MIN_RET
                  and current_score is not None
                  and current_score < EARLY_EXIT_MAX_SCORE):
                exit_reason = f"Salida anticipada: ret {ret_pct:.1f}% con score {current_score} < {EARLY_EXIT_MAX_SCORE}"
                # Cooldown corto para evitar reentrada inmediata por modo momentum
                # con score bajo (el score bajó por algo — no reentrar en 24h)
                is_stop_loss_exit = False   # cooldown TRAILING_HOURS (24h), no stop
            elif hours_held >= hold_hours:
                if ret_pct >= 0:
                    if current_score is not None and current_score >= PAPER2_MIN_SCORE:
                        if not trailing_active and ret_pct >= trailing_min:
                            trailing_active = True; peak_price = current_price
                    else:
                        exit_reason = f"{hold_hours:.0f}h cumplidas, score {current_score} < {PAPER2_MIN_SCORE}"
                elif hours_held >= max_hours:
                    exit_reason = f"{max_hours:.0f}h negativo {ret_pct:.1f}%"
                    is_stop_loss_exit = True   # pérdida prolongada = cooldown largo

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
                    "pnl": round(pnl_eur, 2),
                    "pnl_eur": round(pnl_eur, 2), "reason": exit_reason,
                    "entry_score": pos.get("entry_score"), "hours_held": round(hours_held, 1),
                    "trailing_active": trailing_active, "trailing_pct": trailing_pct,
                })
                storage.remove_open_position(ticker)
                # Cooldown diferenciado: stop loss / pérdida prolongada → 48h
                #                        trailing / score / tiempo → 24h
                cooldown_h = COOLDOWN_STOP_HOURS if is_stop_loss_exit else COOLDOWN_TRAILING_HOURS
                storage.set_cooldown(ticker, (now_dt + timedelta(hours=cooldown_h)).strftime("%Y-%m-%d %H:%M"))
                _alpaca_pool.submit(alpaca_api.place_order, ticker, "sell", 0)
                _tg_pool.submit(telegram_alerts.alert_trade_close, ticker, pos.get("name",""), round(ret_pct,2), round(pnl_eur,2), exit_reason, "EUR")
            else:
                # ── Pyramiding ────────────────────────────────────────────────
                # Añadir capital cuando la posición confirma momentum alcista
                capital_now = storage.get_capital()
                pyra_size = capital_now * PYRA_SIZE_PCT
                can_pyramid = (
                    PYRA_ENABLED
                    and pyramid_count < PYRA_MAX_PER_POS
                    and ret_pct >= PYRA_MIN_RET
                    and hours_held >= PYRA_MIN_HOURS
                    and current_score is not None and current_score >= PYRA_MIN_SCORE
                    and pyra_size >= 1.0
                    and not trailing_active   # no pyramidar si ya está en trailing
                )
                if can_pyramid:
                    fx = get_eurusd()
                    if currency == "USD" and fx > 0:
                        extra_native_size = pyra_size / fx
                        extra_shares = extra_native_size / current_price if current_price > 0 else 0
                    else:
                        extra_shares = pyra_size / current_price if current_price > 0 else 0
                    if extra_shares > 0:
                        native_pyra_cost = pyra_size / fx if (currency == "USD" and fx > 0) else pyra_size
                        storage.atomic_pyramid(ticker, extra_shares, native_pyra_cost)
                        storage.add_capital(-pyra_size)  # deducir el coste en EUR del capital
                        pyramid_count += 1
                        notional_usd = pyra_size / fx if (currency == "USD" and fx > 0) else pyra_size
                        if ticker in ALPACA_TRADEABLE and alpaca_api.alpaca_enabled():
                            _alpaca_pool.submit(alpaca_api.place_order, ticker, "buy", notional_usd)
                        _tg_pool.submit(
                            telegram_alerts.alert_trade_open,
                            ticker, pos.get("name",""), current_score,
                            round(current_price, 2), currency,
                        )
                        print(f"[pyra] {ticker} pyramid #{pyramid_count} +{extra_shares:.4f} shares @ {current_price:.2f} ret={ret_pct:.1f}%")

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
        regime      = regime_info.get("regime", "dip_buying")

        # ── Liberación anticipada de cooldowns por score ───────────────────────
        # Si un ticker en cooldown de trailing (24h) ya tiene score >=85 tras 12h,
        # liberarlo para no perder la señal
        now_dt_check = datetime.now()
        all_cooldowns = storage.get_cooldowns_trailing()
        for cd_ticker, until_str in list(all_cooldowns.items()):
            try:
                until_dt = datetime.strptime(until_str, "%Y-%m-%d %H:%M")
                # Estimar inicio del cooldown: until - COOLDOWN_TRAILING_HOURS
                start_dt = until_dt - timedelta(hours=COOLDOWN_TRAILING_HOURS)
                hours_in_cooldown = (now_dt_check - start_dt).total_seconds() / 3600
                if hours_in_cooldown < COOLDOWN_EARLY_CHECK_HOURS:
                    continue  # demasiado pronto para revisar
                # Solo liberar cooldowns de trailing (duración == COOLDOWN_TRAILING_HOURS)
                total_cooldown = (until_dt - start_dt).total_seconds() / 3600
                if abs(total_cooldown - COOLDOWN_TRAILING_HOURS) > 1:
                    continue  # es un cooldown de stop loss (48h) — no tocar
                # Comprobar score actual del ticker en el cache de mercado
                cd_row = ticker_map.get(cd_ticker)
                if cd_row and cd_row.get("inv_score", 0) >= COOLDOWN_EARLY_MIN_SCORE:
                    storage.remove_cooldown(cd_ticker)
                    print(f"[cooldown] {cd_ticker} liberado anticipadamente — score {cd_row['inv_score']} >= {COOLDOWN_EARLY_MIN_SCORE}")
            except Exception:
                continue

        # Nuevas entradas — modo dual
        cooldowns    = storage.get_cooldowns()
        open_tickers = storage.get_open_tickers()
        capital      = storage.get_capital()

        # ── VIX Circuit Breaker ───────────────────────────────────────────────
        # Si VIX SMA5 > 35, pausar TODAS las nuevas entradas.
        # Las señales de sobreventa en pánico real son trampas — el mercado sigue cayendo.
        # El sistema sigue gestionando posiciones ya abiertas (stops, trailing, etc.)
        vix_now = get_vix_sma5() if VIX_CIRCUIT_BREAKER else None
        vix_breaker_active = (
            VIX_CIRCUIT_BREAKER
            and vix_now is not None
            and vix_now > VIX_CIRCUIT_BREAKER_LEVEL
        )
        if vix_breaker_active:
            print(f"[vix-breaker] VIX SMA5={vix_now:.1f} > {VIX_CIRCUIT_BREAKER_LEVEL} — nuevas entradas pausadas")

        # Base para position sizing: capital inicial fijo (evita shrinkage)
        pos_base = PAPER2_INITIAL_CAP if POS_SIZE_ON_INITIAL_CAP else capital

        if not vix_breaker_active:
            for ticker, row in ticker_map.items():
                if ticker in NON_TRADEABLE: continue
                if ticker in cooldowns or ticker in open_tickers: continue
                if not is_market_open(ticker): continue

                score = row.get("inv_score")

                is_dip  = score is not None and score >= PAPER2_MIN_SCORE
                is_mom  = regime == "momentum" and is_momentum_candidate(row)
                trade_mode = "dip_buying" if is_dip else ("momentum" if is_mom else None)

                # ── Estrategia: RSI Divergence ────────────────────────────────
                # Divergencia RSI alcista = señal de alta convicción aunque score < 85
                if (RSI_DIV_ENABLED and not is_dip
                        and score is not None and score >= RSI_DIV_MIN_SCORE
                        and row.get("rsi_divergence") == "bullish"):
                    is_dip = True
                    trade_mode = "rsi_divergence"

                # ── Estrategia: Dividend Yield Filter ────────────────────────
                # Inversores institucionales sostienen el precio cuando el yield es alto
                elif (DIV_YIELD_ENABLED and not is_dip
                        and score is not None and score >= DIV_MIN_SCORE
                        and (row.get("div_yield") or 0) >= DIV_YIELD_MIN_PCT):
                    is_dip = True
                    trade_mode = "div_yield"

                # ── Estrategia: Bonds score reducido ─────────────────────────
                # TLT/SHY/AGG raramente llegan a 85 — umbral propio más bajo
                elif (BONDS_ENABLED and not is_dip
                        and ticker in BONDS_TICKERS
                        and score is not None and score >= BONDS_MIN_SCORE):
                    is_dip = True
                    trade_mode = "bonds"

                if not is_dip and not is_mom:
                    continue

                # ── ATR máximo de entrada ─────────────────────────────────────────
                # Volatilidad extrema = señales falsas + stops amplísimos
                # COIN, TSLA, ARKK con ATR > 6% en pánico son trampas
                atr_entry = calc_atr_pct(ticker)
                if atr_entry is not None and atr_entry > ATR_MAX_ENTRY:
                    continue   # activo demasiado volátil ahora mismo

                # ── Tamaño de posición por volatilidad (ATR) ──────────────────────
                if atr_entry is not None and atr_entry <= POS_VOL_LOW_ATR:
                    pos_pct = POS_PCT_LOW_VOL
                elif atr_entry is not None and atr_entry <= POS_VOL_MED_ATR:
                    pos_pct = POS_PCT_MED_VOL
                else:
                    pos_pct = POS_PCT_HIGH_VOL

                # Tamaño fijo sobre capital inicial — no se reduce con posiciones abiertas
                position_size = pos_base * pos_pct
                # Guard: no entrar si no hay capital real suficiente
                if position_size < 1 or capital < position_size:
                    continue

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
        storage.set_cooldown(ticker, (datetime.now() + timedelta(hours=COOLDOWN_STOP_HOURS)).strftime("%Y-%m-%d %H:%M"))
        _alpaca_pool.submit(alpaca_api.place_order, ticker, "sell", 0)
        _tg_pool.submit(telegram_alerts.alert_trade_close, ticker, pos.get("name", ""), round(ret_pct, 2), round(pnl_eur, 2), "Venta manual", "EUR")
    return {"status": "sold", "cooldown_hours": COOLDOWN_STOP_HOURS}

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
        # Refleja la extensión de hold si aplica (mismo criterio que run_trading)
        if (not is_mom_ro
                and pos.get("score") is not None and pos["score"] >= HOLD_EXT_SCORE
                and ret_pct >= HOLD_EXT_RET_PCT):
            max_hrs_ro = HOLD_EXT_MAX_HOURS
        else:
            max_hrs_ro = MOM_MAX_HOURS if is_mom_ro else PAPER2_MAX_HOURS
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
    regime_info = dict(get_spy_regime())
    regime_info["scoring_regime"] = get_market_regime()
    vix_now = get_vix_sma5()
    regime_info["vix_breaker_active"] = (
        VIX_CIRCUIT_BREAKER
        and vix_now is not None
        and vix_now > VIX_CIRCUIT_BREAKER_LEVEL
    )
    return {
        "capital": round(capital, 2), "open_value": round(open_value, 2),
        "total_equity": total_equity, "total_ret": total_ret, "initial": PAPER2_INITIAL_CAP,
        "open": positions, "closed": storage.get_closed_trades(200),
        "equity_log": storage.get_equity_log(2000),
        "min_score": PAPER2_MIN_SCORE, "hold_hours": PAPER2_HOLD_HOURS,
        "mom_hold_hours": MOM_HOLD_HOURS,
        "regime": regime_info,
    }
