"""
paper_engine.py — Motor de paper trading Score85.
Usa SQLite (storage.py) en vez de JSON.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
_UTC = timezone.utc

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
    PARTIAL_SELL_ENABLED, TRANCHE_1_PCT, TRANCHE_2_PCT,
    NON_TRADEABLE,
    POS_SIZE_ON_INITIAL_CAP,
    EARLY_EXIT_ENABLED, EARLY_EXIT_MIN_HOURS, EARLY_EXIT_MIN_RET, EARLY_EXIT_MAX_SCORE,
    COOLDOWN_EARLY_CHECK_HOURS, COOLDOWN_EARLY_MIN_SCORE,
    VIX_CIRCUIT_BREAKER, VIX_CIRCUIT_BREAKER_LEVEL,
    ATR_MAX_ENTRY, VOL_MIN_ENTRY,
    RSI_DIV_ENABLED, RSI_DIV_MIN_SCORE,
    DIV_YIELD_ENABLED, DIV_YIELD_MIN_PCT, DIV_MIN_SCORE,
    BONDS_ENABLED, BONDS_MIN_SCORE, BONDS_TICKERS,
    SECTOR_LIMIT_ENABLED, SECTOR_MAX_POSITIONS, SECTOR_MAP,
    GAP_FILTER_ENABLED, GAP_FILTER_PCT,
    PROGRESSIVE_TRAILING_ENABLED, PROGRESSIVE_TRAILING,
    RATCHET_ENABLED, RATCHET_LEVELS,
    DCA_ENTRY_ENABLED, DCA_TRANCHE_1_PCT, DCA_TRANCHE_2_PCT,
    DCA_TRANCHE_2_HOURS, DCA_TRANCHE_3_PCT, DCA_TRANCHE_3_HOURS,
    DCA_MIN_SCORE_HOLD,
    # ── V15 ────────────────────────────────────────────────────────────
    IGNORE_SCORE_ON_TRAILING,
    SCORE_SIZING_ENABLED, SCORE_SIZING_BUCKETS, SCORE_SIZING_MAX_PCT,
    BULL_QUIET_PATCH_ENABLED, BULL_QUIET_MIN_SCORE, BULL_QUIET_MIN_REL,
    EARLY_EXIT_REL_DETERIORATION,
    ASYMMETRIC_COOLDOWN_ENABLED,
    COOLDOWN_BIG_LOSS_HOURS, COOLDOWN_SMALL_LOSS_HOURS,
    COOLDOWN_SMALL_WIN_HOURS, COOLDOWN_BIG_WIN_HOURS,
    COOLDOWN_BIG_WIN_THR, COOLDOWN_BIG_LOSS_THR,
    CORRELATION_LIMIT_ENABLED, CORRELATION_GROUPS, CORRELATION_MAX_POSITIONS,
    MOM_TAKE_PROFIT_V15,
    ADR_STOP_ENABLED, ADR_STOP_MULT, ADR_LOOKBACK_DAYS,
    FALSE_BREAKOUT_ENABLED, FALSE_BREAKOUT_MAX_HOURS,
    FALSE_BREAKOUT_TOUCH_PCT, FALSE_BREAKOUT_RETURN_PCT,
    FALSE_BREAKOUT_MIN_VOL_REL,
    RISK_PARITY_ENABLED, RISK_PER_TRADE_EUR,
    META_SIZING_ENABLED, META_SIZING_DD_THRESHOLD, META_SIZING_REDUCTION,
    EARNINGS_BLACKOUT_ENABLED, EARNINGS_BLACKOUT_HOURS,
    LIQUIDITY_SHOCK_ENABLED, LIQUIDITY_SHOCK_VOL_REL, LIQUIDITY_SHOCK_HOURS,
    DD_CIRCUIT_BREAKER_ENABLED, DD_CIRCUIT_DAILY_THRESHOLD,
    DD_CIRCUIT_WEEKLY_THRESHOLD, DD_CIRCUIT_COOLDOWN_HOURS,
    INTRADAY_VIX_OVERRIDE_ENABLED, INTRADAY_VIX_SPIKE_PCT,
)
from indicators import (
    get_eurusd, get_trailing_pct, get_spy_regime, get_market_regime,
    get_vix_sma5, is_momentum_candidate, calc_atr_pct,
    calc_adr_pct, has_earnings_soon, is_vix_intraday_spiking,
)
import alpaca_api
import storage

log = logging.getLogger("tracker")
import telegram_alerts

paper2_lock = threading.Lock()

# Pools compartidos — evitan acumulación de threads daemon en ciclos con muchas operaciones
_tg_pool     = ThreadPoolExecutor(max_workers=3, thread_name_prefix="tg")
_alpaca_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="alpaca")

def is_market_open(ticker):
    now_utc = datetime.now(tz=_UTC)
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
        log.warning("PYRA_MIN_RET(%.1f%%) >= take_profit mínimo(%.1f%%) — pyramid puede colisionar con trailing en %s", PYRA_MIN_RET, min_take_profit, [k for k,v in TAKE_PROFIT_BY_REGIME.items() if v <= PYRA_MIN_RET])
    open_pos = storage.get_open_positions()
    closed = storage.get_closed_trades(1)
    if not open_pos and not closed:
        storage.migrate_from_json(PAPER2_FILE)


# ─────────────────────────────────────────────────────────────────────────────
# V15: Helpers de drawdown — mejoras #14 y #23
# ─────────────────────────────────────────────────────────────────────────────
def _compute_current_drawdown():
    """Drawdown % desde el pico histórico del equity_log. 0.0 si estamos en ATH."""
    try:
        log_rows = storage.get_equity_log(5000)
        if not log_rows:
            return 0.0
        equities = [r.get("equity", 0) for r in log_rows if r.get("equity")]
        if not equities:
            return 0.0
        peak = max(equities)
        current = equities[-1]
        if peak <= 0:
            return 0.0
        dd = (current - peak) / peak * 100
        return dd
    except Exception:
        return 0.0


def _compute_dd_window(hours):
    """DD en las últimas N horas (para circuit breaker daily/weekly)."""
    try:
        log_rows = storage.get_equity_log(5000)
        if not log_rows:
            return 0.0
        cutoff = datetime.now() - timedelta(hours=hours)
        recent = []
        for r in log_rows:
            ts_str = r.get("ts") or r.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = datetime.strptime(ts_str[:16], "%Y-%m-%d %H:%M")
                if ts >= cutoff:
                    recent.append(r.get("equity", 0))
            except Exception:
                continue
        if len(recent) < 2:
            return 0.0
        peak = max(recent)
        current = recent[-1]
        if peak <= 0:
            return 0.0
        return (current - peak) / peak * 100
    except Exception:
        return 0.0


def _score_size_multiplier(score):
    """Multiplicador del pos_pct por bucket de score (#2)."""
    if not SCORE_SIZING_ENABLED or not isinstance(score, (int, float)):
        return 1.0
    for (lo, hi), mult in SCORE_SIZING_BUCKETS.items():
        if lo <= score <= hi:
            return mult
    return 1.0


def _cooldown_hours_for(ret_pct, is_stop_loss_exit):
    """Cooldown asimétrico por resultado (#5)."""
    if not ASYMMETRIC_COOLDOWN_ENABLED:
        return COOLDOWN_STOP_HOURS if is_stop_loss_exit else COOLDOWN_TRAILING_HOURS
    if ret_pct <= COOLDOWN_BIG_LOSS_THR:
        return COOLDOWN_BIG_LOSS_HOURS
    if ret_pct < 0:
        return COOLDOWN_SMALL_LOSS_HOURS
    if ret_pct >= COOLDOWN_BIG_WIN_THR:
        return COOLDOWN_BIG_WIN_HOURS
    return COOLDOWN_SMALL_WIN_HOURS


def _correlation_group_counts(open_tickers):
    """Posiciones abiertas por grupo de correlación (#6).
    Devuelve dict {group_name: count}."""
    counts = {}
    for tk in open_tickers:
        sector = SECTOR_MAP.get(tk)
        if not sector:
            continue
        for group_name, sectors in CORRELATION_GROUPS.items():
            if sector in sectors:
                counts[group_name] = counts.get(group_name, 0) + 1
    return counts


def _get_correlation_groups_for_sector(sector):
    """Grupos de correlación a los que pertenece un sector."""
    groups = []
    for group_name, sectors in CORRELATION_GROUPS.items():
        if sector in sectors:
            groups.append(group_name)
    return groups


def _compute_dynamic_stop(ticker, atr_pct):
    """Stop combinando ATR y ADR (#11). Toma el más permisivo."""
    atr_stop = None
    if atr_pct is not None:
        atr_stop = -round(max(ATR_STOP_MIN, min(ATR_STOP_MAX, ATR_STOP_MULT * atr_pct)), 2)
    if not ADR_STOP_ENABLED:
        return atr_stop if atr_stop is not None else PAPER2_STOP_LOSS
    adr_pct = calc_adr_pct(ticker, lookback=ADR_LOOKBACK_DAYS)
    if adr_pct is None:
        return atr_stop if atr_stop is not None else PAPER2_STOP_LOSS
    adr_stop = -round(max(ATR_STOP_MIN, min(ATR_STOP_MAX, ADR_STOP_MULT * adr_pct)), 2)
    if atr_stop is None:
        return adr_stop
    # Toma el más permisivo (más negativo, o sea más alejado de 0)
    return min(atr_stop, adr_stop)


def _detect_false_breakout(pos, hours_held, ret_pct, current_price, row):
    """(#12) Devuelve True si detecta patrón de false breakout:
    - Dentro de las primeras 4h
    - El peak llegó a +1% pero ahora el precio está a -0.5%
    - Con volumen alto (vol_rel > 1.3)
    """
    if not FALSE_BREAKOUT_ENABLED:
        return False
    if hours_held > FALSE_BREAKOUT_MAX_HOURS:
        return False
    peak_price = pos.get("peak_price", pos["entry_price"])
    entry_price = pos["entry_price"]
    if entry_price <= 0:
        return False
    peak_ret = (peak_price - entry_price) / entry_price * 100
    if peak_ret < FALSE_BREAKOUT_TOUCH_PCT:
        return False  # nunca llegó a tocar +1%
    if ret_pct > FALSE_BREAKOUT_RETURN_PCT:
        return False  # aún no ha caído al nivel crítico
    vol_rel = row.get("vol_rel") if row else None
    if vol_rel is None or vol_rel < FALSE_BREAKOUT_MIN_VOL_REL:
        return False  # volumen normal = no es capitulación
    return True


def _check_liquidity_shock(pos, row):
    """(#17) Si vol_rel < 0.3 sostenido ≥2h → salir. Usa 'liquidity_shock_start'
    persistido en pos para trackear la duración."""
    if not LIQUIDITY_SHOCK_ENABLED or row is None:
        return False, None
    vol_rel = row.get("vol_rel")
    if vol_rel is None:
        return False, pos.get("liquidity_shock_start")
    now_dt = datetime.now()
    shock_start = pos.get("liquidity_shock_start")
    if vol_rel < LIQUIDITY_SHOCK_VOL_REL:
        if shock_start is None:
            # Primer tick con volumen muy bajo — iniciar contador
            return False, now_dt.strftime("%Y-%m-%d %H:%M")
        try:
            start_dt = datetime.strptime(shock_start, "%Y-%m-%d %H:%M")
            duration = (now_dt - start_dt).total_seconds() / 3600
            if duration >= LIQUIDITY_SHOCK_HOURS:
                return True, shock_start
        except Exception:
            return False, None
        return False, shock_start
    else:
        # Volumen recuperado — resetear contador
        return False, None


def _is_dd_circuit_active():
    """(#23) Circuit breaker propio: pausa entradas si hay drawdown severo."""
    if not DD_CIRCUIT_BREAKER_ENABLED:
        return False, None
    daily_dd = _compute_dd_window(24)
    weekly_dd = _compute_dd_window(168)
    if daily_dd <= DD_CIRCUIT_DAILY_THRESHOLD:
        return True, f"daily DD {daily_dd:.1f}% <= {DD_CIRCUIT_DAILY_THRESHOLD}%"
    if weekly_dd <= DD_CIRCUIT_WEEKLY_THRESHOLD:
        return True, f"weekly DD {weekly_dd:.1f}% <= {DD_CIRCUIT_WEEKLY_THRESHOLD}%"
    return False, None


def _effective_min_score(ticker, row, current_regime):
    """Score mínimo aplicable teniendo en cuenta parche BULL_QUIET (#3)."""
    if not BULL_QUIET_PATCH_ENABLED:
        return PAPER2_MIN_SCORE
    if current_regime != "BULL_QUIET":
        return PAPER2_MIN_SCORE
    # En BULL_QUIET exigimos rel_strength suficientemente positivo
    rel = row.get("rel_strength") if row else None
    if rel is not None and rel >= BULL_QUIET_MIN_REL:
        return BULL_QUIET_MIN_SCORE
    return PAPER2_MIN_SCORE


def run_trading(market_data):
    with paper2_lock, storage.batch_transaction():
        now_dt = datetime.now()
        now_str = now_dt.strftime("%Y-%m-%d %H:%M")
        _utc_now = datetime.now(tz=_UTC)  # para checks de horario de mercado
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

            # ── VARIABLES — definir ANTES de usar (fix critical bug) ─────────
            exit_reason = None
            trailing_active = bool(pos.get("trailing_active", False))
            pyramid_count = int(pos.get("pyramid_count", 0))
            tranche_2_done = bool(pos.get("tranche_2_done", False))

            # ── RATCHET FLOOR — suelo de ganancia que solo sube ───────────────
            # Persistido en DB como ratchet_floor (float, % de ganancia mínima)
            ratchet_floor = float(pos.get("ratchet_floor", 0))
            if RATCHET_ENABLED and ret_pct > 0:
                # Recalcular el suelo: iterar niveles de menor a mayor,
                # el suelo es el más alto que ret_pct haya cruzado
                _new_floor = ratchet_floor
                for _threshold, _floor in sorted(RATCHET_LEVELS.items()):
                    if ret_pct >= _threshold:
                        _new_floor = max(_new_floor, _floor)
                # El suelo SOLO sube, nunca baja
                if _new_floor > ratchet_floor:
                    ratchet_floor = _new_floor
                    log.debug("[ratchet] %s ret=%.1f%% → suelo subió a +%.1f%%",
                              ticker, ret_pct, ratchet_floor)

            # ── Parámetros según modo ─────────────────────────────────────────
            is_mom_trade = pos.get("trade_mode") == "momentum"
            hold_hours   = MOM_HOLD_HOURS  if is_mom_trade else PAPER2_HOLD_HOURS
            trailing_min = MOM_TRAILING_MIN if is_mom_trade else PAPER2_TRAILING_MIN

            # ── ATR — una sola llamada, ya cacheado desde fetch_ticker ─────────
            atr_pct = calc_atr_pct(ticker)

            # ── Trailing PCT base (ATR-adaptado) ──────────────────────────────
            trailing_pct = pos.get("trailing_pct") or get_trailing_pct(ticker)

            # ── Trailing progresivo por duración del trade ────────────────────
            # {8: 1.5, 24: 1.2, 48: 0.9, 999: 0.7} → más amplio al inicio, más ajustado después
            if PROGRESSIVE_TRAILING_ENABLED and trailing_active:
                _pt_mult = 1.0
                for _pt_hours, _pt_m in sorted(PROGRESSIVE_TRAILING.items()):
                    if hours_held <= _pt_hours:
                        _pt_mult = _pt_m
                        break
                trailing_pct = round(trailing_pct * _pt_mult, 2)

            # ── Trailing asimétrico por hora de cierre ────────────────────────
            # Si quedan <45min para cierre US y hay ganancia, apretar trailing
            if trailing_active and ret_pct > 0 and ticker not in EU_TICKERS:
                _mins_utc = _utc_now.utctimetuple().tm_hour * 60 + _utc_now.utctimetuple().tm_min
                if 20*60+15 <= _mins_utc <= 21*60:
                    trailing_pct = round(max(trailing_pct * 0.55, 0.8), 2)

            # ── Stop dinámico por ATR + ADR (v15 #11) ────────────────────────
            dynamic_stop = _compute_dynamic_stop(ticker, atr_pct)

            # ── Trailing dinámico por régimen ─────────────────────────────────
            if is_mom_trade:
                # V15 #7: take profit más alto en momentum (ratchet ya protege desde +3%)
                take_profit = MOM_TAKE_PROFIT_V15
            else:
                scoring_regime = get_market_regime()
                take_profit = TAKE_PROFIT_BY_REGIME.get(scoring_regime, PAPER2_TAKE_PROFIT)

            # ── Hold máximo: extensión por score ─────────────────────────────
            if (isinstance(current_score, (int, float)) and current_score >= HOLD_EXT_SCORE
                    and ret_pct >= HOLD_EXT_RET_PCT and not is_mom_trade):
                max_hours = HOLD_EXT_MAX_HOURS
            else:
                max_hours = MOM_MAX_HOURS if is_mom_trade else PAPER2_MAX_HOURS

            # ── Lógica de salida ──────────────────────────────────────────────
            # V15 #17: liquidity shock check (resetea peak si vuelve el volumen)
            liq_shock_exit, liq_shock_start_new = _check_liquidity_shock(pos, row)

            if not trailing_active and ret_pct > take_profit:
                trailing_active = True
                peak_price = current_price
                # ── Tramo 1: venta parcial al activar trailing ────────────────
                if PARTIAL_SELL_ENABLED and pos["shares"] > 0:
                    shares_to_sell  = round(pos["shares"] * TRANCHE_1_PCT, 6)
                    shares_to_sell  = min(shares_to_sell, pos["shares"])
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
                        "reason": f"Tramo 1: {int(TRANCHE_1_PCT*100)}% al activar trailing",
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
                        f"Tramo 1 ({int(TRANCHE_1_PCT*100)}%)", "EUR"
                    )
                    if ticker in ALPACA_TRADEABLE and alpaca_api.alpaca_enabled():
                        _alpaca_pool.submit(
                            alpaca_api.place_order, ticker, "sell", 0,
                            shares_to_sell   # qty para venta parcial
                        )
                    log.info("[tranche1] %s vendido %d%% @ %.2f ret=%.1f%%", ticker, int(TRANCHE_1_PCT*100), current_price, ret_pct)

            # ── Tramo 2: venta parcial al 50% del trailing stop ────────────
            # Si el trailing está activo y ha devuelto la mitad del trailing_pct,
            # vender otro 33% para proteger más ganancia
            if (PARTIAL_SELL_ENABLED and trailing_active and not tranche_2_done
                    and pos["shares"] > 0 and trailing_drop <= -(trailing_pct * 0.5)):
                _t2_shares = round(pos["shares"] * TRANCHE_2_PCT / (1.0 - TRANCHE_1_PCT), 6)
                _t2_shares = min(_t2_shares, pos["shares"])
                if _t2_shares > 0:
                    _t2_eur = _t2_shares * _to_eur(current_price, currency)
                    _t2_pnl = _t2_shares * (_to_eur(current_price, currency) - _to_eur(pos["entry_price"], currency))
                    storage.partial_close(ticker, _t2_shares, _t2_eur)
                    storage.add_closed_trade({
                        "ticker": ticker, "name": pos.get("name"),
                        "entry_date": pos["entry_date"], "exit_date": now_str,
                        "entry_price": pos["entry_price"], "exit_price": round(current_price, 2),
                        "peak_price": round(peak_price, 2), "currency": currency,
                        "shares": round(_t2_shares, 6), "ret_pct": round(ret_pct, 2),
                        "pnl": round(_t2_pnl, 2), "pnl_eur": round(_t2_pnl, 2),
                        "reason": f"Tramo 2: {int(TRANCHE_2_PCT*100)}% trailing -50%",
                        "entry_score": pos.get("entry_score"), "hours_held": round(hours_held, 1),
                        "trailing_active": True, "trailing_pct": trailing_pct, "is_partial": True,
                    })
                    pos = dict(pos)
                    pos["shares"] = pos["shares"] - _t2_shares
                    tranche_2_done = True
                    _tg_pool.submit(telegram_alerts.alert_trade_close, ticker, pos.get("name",""),
                                    round(ret_pct, 2), round(_t2_pnl, 2), f"Tramo 2 ({int(TRANCHE_2_PCT*100)}%)", "EUR")
                    if ticker in ALPACA_TRADEABLE and alpaca_api.alpaca_enabled():
                        _alpaca_pool.submit(alpaca_api.place_order, ticker, "sell", 0, _t2_shares)
                    log.info("[tranche2] %s vendido %d%% @ %.2f ret=%.1f%%", ticker, int(TRANCHE_2_PCT*100), current_price, ret_pct)

            # Cooldown diferenciado: stop loss = fallo real (48h), trailing/score/tiempo = OK (24h)
            is_stop_loss_exit = False
            if ret_pct <= dynamic_stop:
                exit_reason = f"Stop loss {ret_pct:.1f}% (stop {dynamic_stop:.1f}%)"
                is_stop_loss_exit = True
            # V15 #12 — false breakout: salir ANTES de que llegue al stop
            elif _detect_false_breakout(pos, hours_held, ret_pct, current_price, row):
                exit_reason = f"False breakout: peak alcanzado pero retornó a {ret_pct:.1f}% con vol alto"
                is_stop_loss_exit = False  # salida técnica, no stop completo
            # V15 #17 — liquidity shock: 2h con vol_rel < 0.3
            elif liq_shock_exit:
                exit_reason = f"Liquidity shock: vol_rel < {LIQUIDITY_SHOCK_VOL_REL} durante >{LIQUIDITY_SHOCK_HOURS}h"
                is_stop_loss_exit = False
            # ── Ratchet floor: si ret cayó por debajo del suelo garantizado ──
            elif RATCHET_ENABLED and ratchet_floor > 0 and ret_pct < ratchet_floor:
                exit_reason = f"Ratchet floor: ret {ret_pct:.1f}% < suelo +{ratchet_floor:.1f}%"
            elif trailing_active and trailing_drop <= -trailing_pct:
                exit_reason = f"Trailing stop {trailing_drop:.1f}% (ATR:{trailing_pct}%)"
            # V15 #1: NO cerrar por score bajo si el trailing ya está activo
            # El score baja naturalmente cuando el ticker rebota — es señal de éxito, no de peligro
            elif (not IGNORE_SCORE_ON_TRAILING
                  and trailing_active and isinstance(current_score, (int, float)) and current_score < PAPER2_MIN_SCORE):
                exit_reason = f"Score bajó a {current_score}"
            # ── Salida anticipada dinámica (#4 reforzada con rel_strength) ───
            # Solo disparar si además de score bajo hay deterioro real de rel_strength
            elif (EARLY_EXIT_ENABLED
                  and not trailing_active
                  and hours_held >= EARLY_EXIT_MIN_HOURS
                  and ret_pct >= EARLY_EXIT_MIN_RET
                  and current_score is not None
                  and current_score < EARLY_EXIT_MAX_SCORE):
                # V15 #4: confirmar con deterioro de rel_strength vs entrada
                rel_now = row.get("rel_strength") if row else None
                rel_entry = pos.get("entry_rel_strength")
                rel_ok_to_exit = True  # por defecto sí, si no hay datos de rel
                if rel_entry is not None and rel_now is not None:
                    rel_delta = rel_now - rel_entry
                    # Solo salir si rel empeoró en más de EARLY_EXIT_REL_DETERIORATION
                    rel_ok_to_exit = (rel_delta <= -EARLY_EXIT_REL_DETERIORATION)
                if rel_ok_to_exit:
                    exit_reason = f"Salida anticipada: ret {ret_pct:.1f}% con score {current_score} < {EARLY_EXIT_MAX_SCORE}"
                    is_stop_loss_exit = False
            elif hours_held >= hold_hours:
                if ret_pct >= 0:
                    if isinstance(current_score, (int, float)) and current_score >= PAPER2_MIN_SCORE:
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
                # Cooldown asimétrico por resultado (V15 #5)
                cooldown_h = _cooldown_hours_for(ret_pct, is_stop_loss_exit)
                storage.set_cooldown(ticker, (now_dt + timedelta(hours=cooldown_h)).strftime("%Y-%m-%d %H:%M"))
                _alpaca_pool.submit(alpaca_api.place_order, ticker, "sell", 0)
                _tg_pool.submit(telegram_alerts.alert_trade_close, ticker, pos.get("name",""), round(ret_pct,2), round(pnl_eur,2), exit_reason, "EUR")
            else:
                # ── DCA: ejecutar tramos pendientes ───────────────────────────
                # Tramo 2 tras DCA_TRANCHE_2_HOURS si score se mantiene
                # Tramo 3 tras DCA_TRANCHE_3_HOURS si score se mantiene
                _dca_pending = pos.get("dca_pending_eur")
                _dca_tranche = int(pos.get("dca_tranche", 3))  # 3 = completado
                if DCA_ENTRY_ENABLED and _dca_pending and _dca_tranche < 3:
                    try:
                        _total_eur = float(_dca_pending)
                    except (TypeError, ValueError):
                        _total_eur = 0
                    _should_exec = False
                    _tranche_pct = 0
                    if _dca_tranche == 1 and hours_held >= DCA_TRANCHE_2_HOURS:
                        _tranche_pct = DCA_TRANCHE_2_PCT
                        _should_exec = True
                        _next_tranche = 2
                    elif _dca_tranche == 2 and hours_held >= DCA_TRANCHE_3_HOURS:
                        _tranche_pct = DCA_TRANCHE_3_PCT
                        _should_exec = True
                        _next_tranche = 3
                    if (_should_exec and _total_eur > 0
                            and isinstance(current_score, (int, float))
                            and current_score >= DCA_MIN_SCORE_HOLD):
                        _dca_eur = _total_eur * _tranche_pct
                        _cap_now = storage.get_capital()
                        if _dca_eur <= _cap_now and _dca_eur >= 1:
                            _fx = get_eurusd()
                            if currency == "USD" and _fx > 0:
                                _dca_native = _dca_eur / _fx
                                _dca_shares = _dca_native / current_price if current_price > 0 else 0
                            else:
                                _dca_shares = _dca_eur / current_price if current_price > 0 else 0
                                _dca_native = _dca_eur
                            if _dca_shares > 0:
                                storage.atomic_pyramid(ticker, _dca_shares, _dca_native)
                                storage.add_capital(-_dca_eur)
                                _dca_tranche = _next_tranche
                                log.info("[dca] %s tranche %d +%.4f shares @ %.2f", ticker, _next_tranche, _dca_shares, current_price)
                    elif _should_exec and isinstance(current_score, (int, float)) and current_score < DCA_MIN_SCORE_HOLD:
                        # Score cayó — cancelar DCA restante
                        _dca_tranche = 3
                        log.info("[dca] %s cancelado — score %s < %s", ticker, current_score, DCA_MIN_SCORE_HOLD)

                # ── Pyramiding ────────────────────────────────────────────────
                # Añadir capital cuando la posición confirma momentum alcista
                capital_now = storage.get_capital()
                pyra_size = capital_now * PYRA_SIZE_PCT
                can_pyramid = (
                    PYRA_ENABLED
                    and pyramid_count < PYRA_MAX_PER_POS
                    and ret_pct >= PYRA_MIN_RET
                    and hours_held >= PYRA_MIN_HOURS
                    and isinstance(current_score, (int, float)) and current_score >= PYRA_MIN_SCORE
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
                        _row = ticker_map.get(ticker, {})
                        _bd = _row.get("inv_score_breakdown")
                        _bd_dict = _bd if isinstance(_bd, dict) else None
                        _tg_pool.submit(
                            telegram_alerts.alert_trade_open,
                            ticker, pos.get("name",""), current_score,
                            round(current_price, 2), currency,
                            _bd_dict, "pyramid",
                        )
                        log.info("[pyra] %s pyramid #%d +%.4f shares @ %.2f ret=%.1f%%", ticker, pyramid_count, extra_shares, current_price, ret_pct)

                storage.update_open_position(ticker, {
                    "current_price": round(current_price, 2), "peak_price": round(peak_price, 4),
                    "ret_pct": round(ret_pct, 2), "hours_held": round(hours_held, 1),
                    "hours_left": round(max(0, hold_hours - hours_held), 1),
                    "score": current_score, "trailing_active": int(trailing_active),
                    "trailing_pct": trailing_pct, "trailing_drop": round(trailing_drop, 2),
                    "waiting_recovery": int(hours_held >= hold_hours and ret_pct < 0 and hours_held < max_hours),
                    "tranche_2_done": int(tranche_2_done),
                    "dca_tranche": _dca_tranche if DCA_ENTRY_ENABLED else int(pos.get("dca_tranche", 3)),
                    "ratchet_floor": round(ratchet_floor, 2),
                    "liquidity_shock_start": liq_shock_start_new,   # V15 #17
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
                    log.info("[cooldown] %s liberado anticipadamente — score %s >= %s", cd_ticker, cd_row["inv_score"], COOLDOWN_EARLY_MIN_SCORE)
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
            log.warning("[vix-breaker] VIX SMA5=%.1f > %.1f — nuevas entradas pausadas", vix_now, VIX_CIRCUIT_BREAKER_LEVEL)

        # ── V15 #23: DD Circuit Breaker propio ─────────────────────────────────
        dd_breaker_active, dd_reason = _is_dd_circuit_active()
        if dd_breaker_active:
            log.warning("[dd-breaker] %s — nuevas entradas pausadas", dd_reason)

        # ── V15 #14: Meta-sizing por drawdown ──────────────────────────────────
        current_dd = _compute_current_drawdown()
        meta_mult = 1.0
        if META_SIZING_ENABLED and current_dd <= META_SIZING_DD_THRESHOLD:
            meta_mult = META_SIZING_REDUCTION
            log.info("[meta-sizing] DD %.1f%% <= %.1f%% → sizing %.0f%%",
                     current_dd, META_SIZING_DD_THRESHOLD, meta_mult * 100)

        # Base para position sizing: capital inicial fijo (evita shrinkage)
        pos_base = PAPER2_INITIAL_CAP if POS_SIZE_ON_INITIAL_CAP else capital

        # ── V15 #6: pre-calcular correlation group counts ──────────────────────
        corr_group_counts = _correlation_group_counts(open_tickers) if CORRELATION_LIMIT_ENABLED else {}

        if not vix_breaker_active and not dd_breaker_active:
            for ticker, row in ticker_map.items():
                if ticker in NON_TRADEABLE: continue
                if ticker in cooldowns or ticker in open_tickers: continue
                if not is_market_open(ticker): continue

                # ── MEJORA: Filtro de hora de entrada ────────────────────────
                # Evitar entradas en los primeros y últimos 30 min del mercado US
                # (alta volatilidad espuria, spread elevado, órdenes institucionales de apertura/cierre)
                if ticker not in EU_TICKERS and ticker not in FUTURES_TICKERS:
                    _t_utc = _utc_now.utctimetuple()
                    _mins  = _t_utc.tm_hour * 60 + _t_utc.tm_min
                    # US open: 13:30-14:00 UTC y close: 20:30-21:00 UTC
                    if 13*60+30 <= _mins <= 14*60 or 20*60+30 <= _mins <= 21*60:
                        continue  # zona de ruido — saltar entrada

                # Releer capital de DB en cada iteración para evitar desincronización
                # si una entrada previa falló a mitad o si el ciclo tarda varios segundos
                capital = storage.get_capital()

                score = row.get("inv_score")

                # V15 #3: umbral efectivo (puede bajar a BULL_QUIET_MIN_SCORE en bull tranquilo con rel fuerte)
                effective_min_score = _effective_min_score(ticker, row, get_market_regime())

                is_dip  = isinstance(score, (int, float)) and score >= effective_min_score
                is_mom  = regime == "momentum" and is_momentum_candidate(row)
                # trade_mode siempre uno de los conocidos (dip_buying/momentum/...)
                # — el patch se identifica por log, no por nuevo modo
                trade_mode = "dip_buying" if is_dip else ("momentum" if is_mom else None)
                _patched = (is_dip and BULL_QUIET_PATCH_ENABLED
                            and effective_min_score < PAPER2_MIN_SCORE
                            and isinstance(score, (int, float))
                            and score < PAPER2_MIN_SCORE)
                if _patched:
                    log.info("[bull_quiet_patch] %s entrada con score %s (umbral relajado %s)",
                             ticker, score, effective_min_score)

                # ── Estrategia: RSI Divergence ────────────────────────────────
                # Divergencia RSI alcista = señal de alta convicción aunque score < 85
                if (RSI_DIV_ENABLED and not is_dip
                        and isinstance(score, (int, float)) and score >= RSI_DIV_MIN_SCORE
                        and row.get("rsi_divergence") == "bullish"):
                    is_dip = True
                    trade_mode = "rsi_divergence"

                # ── Estrategia: Dividend Yield Filter ────────────────────────
                # Inversores institucionales sostienen el precio cuando el yield es alto
                elif (DIV_YIELD_ENABLED and not is_dip
                        and isinstance(score, (int, float)) and score >= DIV_MIN_SCORE
                        and (row.get("div_yield") or 0) >= DIV_YIELD_MIN_PCT):
                    is_dip = True
                    trade_mode = "div_yield"

                # ── Estrategia: Bonds score reducido ─────────────────────────
                # TLT/SHY/AGG raramente llegan a 85 — umbral propio más bajo
                elif (BONDS_ENABLED and not is_dip
                        and ticker in BONDS_TICKERS
                        and isinstance(score, (int, float)) and score >= BONDS_MIN_SCORE):
                    is_dip = True
                    trade_mode = "bonds"

                if not is_dip and not is_mom:
                    continue

                # ── MEJORA: Filtro de volumen mínimo ─────────────────────────
                # vol_rel < 0.6 = señal poco fiable + spread elevado
                # Las señales con volumen muy bajo tienen ~40% menos de WR histórico
                _vol_rel = row.get("vol_rel")
                if _vol_rel is not None and _vol_rel < VOL_MIN_ENTRY:
                    continue  # volumen insuficiente — señal poco fiable

                # ── ATR máximo de entrada ─────────────────────────────────────────
                # Volatilidad extrema = señales falsas + stops amplísimos
                # COIN, TSLA, ARKK con ATR > 6% en pánico son trampas
                atr_entry = calc_atr_pct(ticker)
                if atr_entry is not None and atr_entry > ATR_MAX_ENTRY:
                    continue   # activo demasiado volátil ahora mismo

                # ── Límite de exposición sectorial ────────────────────────────────
                # Máximo SECTOR_MAX_POSITIONS posiciones por sector GICS
                if SECTOR_LIMIT_ENABLED and ticker in SECTOR_MAP:
                    _sector = SECTOR_MAP[ticker]
                    _sector_count = sum(
                        1 for _ot in open_tickers
                        if SECTOR_MAP.get(_ot) == _sector
                    )
                    if _sector_count >= SECTOR_MAX_POSITIONS:
                        continue   # sector saturado

                # ── V15 #6: Correlation group limit ──────────────────────────────
                if CORRELATION_LIMIT_ENABLED and ticker in SECTOR_MAP:
                    _sector_of_ticker = SECTOR_MAP[ticker]
                    _groups_of_ticker = _get_correlation_groups_for_sector(_sector_of_ticker)
                    _saturated = False
                    for _group in _groups_of_ticker:
                        if corr_group_counts.get(_group, 0) >= CORRELATION_MAX_POSITIONS:
                            _saturated = True
                            break
                    if _saturated:
                        continue  # grupo correlacionado saturado

                # ── V15 #15: Earnings blackout ───────────────────────────────────
                if EARNINGS_BLACKOUT_ENABLED and ticker in ALPACA_TRADEABLE:
                    if has_earnings_soon(ticker, hours_ahead=EARNINGS_BLACKOUT_HOURS):
                        log.debug("[earnings] %s skip — earnings en <%dh", ticker, EARNINGS_BLACKOUT_HOURS)
                        continue

                # ── Filtro de gap overnight ───────────────────────────────────────
                # Gap down > 3% = noticias negativas (earnings miss, downgrades)
                # RSI oversold en estos casos no es fiable como en caídas graduales
                if GAP_FILTER_ENABLED:
                    _gap = row.get("gap_down_pct")
                    if _gap is not None and _gap < GAP_FILTER_PCT:
                        continue   # gap down excesivo — señal poco fiable

                # ── Tamaño de posición por volatilidad (ATR) ──────────────────────
                if atr_entry is not None and atr_entry <= POS_VOL_LOW_ATR:
                    pos_pct = POS_PCT_LOW_VOL
                elif atr_entry is not None and atr_entry <= POS_VOL_MED_ATR:
                    pos_pct = POS_PCT_MED_VOL
                else:
                    pos_pct = POS_PCT_HIGH_VOL

                # V15 #2: multiplicador por bucket de score
                score_mult = _score_size_multiplier(score)
                pos_pct = pos_pct * score_mult

                # V15 #14: meta-sizing por drawdown
                pos_pct = pos_pct * meta_mult

                # Cap absoluto
                pos_pct = min(pos_pct, SCORE_SIZING_MAX_PCT)

                # Tamaño fijo sobre capital inicial — no se reduce con posiciones abiertas
                position_size = pos_base * pos_pct
                # Guard: no entrar si no hay capital real suficiente
                if position_size < 1 or capital < position_size:
                    continue

                currency = "EUR"  # valor por defecto — evita shadowing entre iteraciones
                fx = get_eurusd()
                if ticker in ALPACA_TRADEABLE and fx > 0:
                    native_price = round(row["price"] / fx, 4)
                    currency = "USD"
                else:
                    native_price = row["price"]

                # shares se calcula sobre el precio nativo para que el P&L
                # (shares × Δnative) sea consistente con la divisa de entry_price.
                # position_size está en EUR → convertimos al nativo antes de dividir.
                # ── DCA: entrada escalonada en 2-3 tramos ────────────────────────
                # Tramo 1 inmediato, tramos 2-3 pendientes (se ejecutan en ciclos futuros)
                if DCA_ENTRY_ENABLED:
                    _dca_size = position_size * DCA_TRANCHE_1_PCT
                else:
                    _dca_size = position_size
                native_position_size = _dca_size / fx if (currency == "USD" and fx > 0) else _dca_size
                shares       = native_position_size / native_price if native_price > 0 else 0
                notional_usd = _dca_size / fx if fx > 0 else _dca_size
                trailing_pct_entry = get_trailing_pct(ticker)

                _dca_pending = None
                if DCA_ENTRY_ENABLED:
                    _dca_pending = f"{position_size:.2f}"  # total position size en EUR

                storage.atomic_open_position({
                    "ticker": ticker, "name": row["name"], "entry_date": now_str,
                    "entry_price": native_price, "current_price": native_price,
                    "peak_price": native_price, "currency": currency,
                    "shares": round(shares, 6), "ret_pct": 0.0, "trailing_drop": 0.0,
                    "trailing_active": False, "trailing_pct": trailing_pct_entry,
                    "hours_held": 0.0, "hours_left": float(PAPER2_HOLD_HOURS),
                    "entry_score": score, "score": score, "waiting_recovery": False,
                    "trade_mode": trade_mode,
                    "dca_pending_eur": _dca_pending,
                    "dca_tranche": 1,
                    # V15: snapshot para early exit con rel_strength
                    "entry_rel_strength": row.get("rel_strength"),
                }, cost_eur=_dca_size)
                capital -= _dca_size
                open_tickers.add(ticker)
                # Actualizar contadores de grupo tras esta entrada
                if CORRELATION_LIMIT_ENABLED and ticker in SECTOR_MAP:
                    _sec = SECTOR_MAP[ticker]
                    for _g in _get_correlation_groups_for_sector(_sec):
                        corr_group_counts[_g] = corr_group_counts.get(_g, 0) + 1
                if ticker in ALPACA_TRADEABLE and alpaca_api.alpaca_enabled():
                    _alpaca_pool.submit(alpaca_api.place_order, ticker, "buy", notional_usd)
                _tg_pool.submit(
                    telegram_alerts.alert_trade_open,
                    ticker, row["name"], score, native_price, currency,
                    row.get("inv_score_breakdown"), trade_mode,
                )

        # ── Actualizar watchlist — tickers cerca del umbral (score 70-84) ──────
        try:
            storage.clean_watchlist_stale(hours=48)
            _watchlist_threshold_low  = 70
            _watchlist_threshold_high = PAPER2_MIN_SCORE - 1  # 84
            for _tk, _row in ticker_map.items():
                _sc = _row.get("inv_score")
                if (isinstance(_sc, (int, float))
                        and _watchlist_threshold_low <= _sc <= _watchlist_threshold_high
                        and _tk not in open_tickers
                        and _tk not in cooldowns
                        and _tk not in NON_TRADEABLE):
                    storage.upsert_watchlist(
                        _tk, _row.get("name", _tk), int(_sc),
                        _row.get("signal", ""), _row.get("rsi"), _row.get("bb_pct")
                    )
        except Exception as _we:
            log.debug("watchlist update error: %s", _we)

        # Equity log — convertir a EUR para consistencia
        positions = storage.get_open_positions()
        open_value_eur = sum(
            p["shares"] * _to_eur(
                ticker_map[p["ticker"]]["price"] if p["ticker"] in ticker_map else p["entry_price"],
                p.get("currency", "EUR")
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
        # Siempre convertir a EUR para P&L consistente — row["price"] puede ser USD
        eur_exit_price  = _to_eur(current_price, currency)
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
        storage.set_cooldown(ticker, (datetime.now() + timedelta(hours=_cooldown_hours_for(ret_pct, False))).strftime("%Y-%m-%d %H:%M"))
        _alpaca_pool.submit(alpaca_api.place_order, ticker, "sell", 0)
        _tg_pool.submit(telegram_alerts.alert_trade_close, ticker, pos.get("name", ""), round(ret_pct, 2), round(pnl_eur, 2), "Venta manual", "EUR")
    return {"status": "sold", "cooldown_hours": COOLDOWN_TRAILING_HOURS}

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
        current_price = _to_native(row["price"], currency)
        ret_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
        # Refleja la extensión de hold si aplica (mismo criterio que run_trading)
        if (not is_mom_ro
                and pos.get("score") is not None and pos["score"] >= HOLD_EXT_SCORE
                and ret_pct >= HOLD_EXT_RET_PCT):
            max_hrs_ro = HOLD_EXT_MAX_HOURS
        else:
            max_hrs_ro = MOM_MAX_HOURS if is_mom_ro else PAPER2_MAX_HOURS
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
        # Ratchet floor — recalcular para la vista (misma lógica que run_trading)
        _rf = float(pos.get("ratchet_floor", 0))
        if RATCHET_ENABLED and ret_pct > 0:
            for _thr, _flr in sorted(RATCHET_LEVELS.items()):
                if ret_pct >= _thr:
                    _rf = max(_rf, _flr)
        pos["ratchet_floor"] = round(_rf, 2)

    # open_value en EUR: usar current_price ya calculado (en divisa nativa) y convertir
    open_value = sum(
        p["shares"] * _to_eur(
            p.get("current_price", p["entry_price"]),
            p.get("currency", "EUR")
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
        # ── V15: métricas nuevas para la UI ────────────────────────────────
        "v15": {
            "current_drawdown": round(_compute_current_drawdown(), 2),
            "daily_drawdown":   round(_compute_dd_window(24), 2),
            "weekly_drawdown":  round(_compute_dd_window(168), 2),
            "meta_sizing_active": (META_SIZING_ENABLED and _compute_current_drawdown() <= META_SIZING_DD_THRESHOLD),
            "dd_breaker_active":  _is_dd_circuit_active()[0],
            "correlation_counts": _correlation_group_counts(storage.get_open_tickers()) if CORRELATION_LIMIT_ENABLED else {},
            "vix_intraday_spike": is_vix_intraday_spiking(INTRADAY_VIX_SPIKE_PCT) if INTRADAY_VIX_OVERRIDE_ENABLED else False,
        },
    }
