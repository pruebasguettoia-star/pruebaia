"""
alpaca_api.py — Integración con Alpaca para órdenes de paper/live trading.
"""
import json
import logging
import urllib.request
from config import alpaca_enabled, alpaca_key, alpaca_secret, alpaca_url, ALPACA_TRADEABLE

log = logging.getLogger("tracker")


def _headers():
    return {
        "APCA-API-KEY-ID":     alpaca_key(),
        "APCA-API-SECRET-KEY": alpaca_secret(),
        "Content-Type":        "application/json",
    }


def place_order(ticker, side, notional_usd, qty=None):
    """Envía orden de mercado. side='buy'|'sell'. Devuelve order_id o None.
    Para ventas parciales pasar qty=shares_to_sell (float).
    """
    if not alpaca_enabled():
        log.debug("alpaca desactivado — configura ALPACA_API_KEY y ALPACA_SECRET_KEY")
        return None
    if ticker not in ALPACA_TRADEABLE:
        log.debug("alpaca: %s no operable en Alpaca — omitiendo", ticker)
        return None
    try:
        payload = {"symbol": ticker, "side": side, "type": "market", "time_in_force": "gtc"}
        if side == "buy":
            existing = get_position_qty(ticker)
            if existing is not None and float(existing) > 0:
                log.debug("alpaca: %s ya tiene posición (%s shares) — omitiendo", ticker, existing)
                return None
            payload["notional"] = str(round(notional_usd, 2))
        else:
            if qty is not None and float(qty) > 0:
                # Venta parcial — qty explícito
                payload["qty"] = str(round(float(qty), 6))
            else:
                # Venta total — usar qty de la posición en Alpaca
                pos_qty = get_position_qty(ticker)
                if pos_qty is None or float(pos_qty) <= 0:
                    log.warning("alpaca: no hay posición para %s", ticker)
                    return None
                payload["qty"] = pos_qty

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{alpaca_url()}/v2/orders", data=data, headers=_headers(), method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        oid = result.get("id")
        log.info("alpaca: %s %s — id:%s status:%s", side, ticker, oid, result.get("status"))
        return oid
    except Exception as e:
        log.error("alpaca error %s %s: %s", side, ticker, e)
        return None


def get_position_qty(ticker):
    try:
        req = urllib.request.Request(
            f"{alpaca_url()}/v2/positions/{ticker}", headers=_headers(), method="GET"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("qty")
    except Exception:
        return None


def get_account():
    if not alpaca_enabled():
        return None
    try:
        req = urllib.request.Request(
            f"{alpaca_url()}/v2/account", headers=_headers(), method="GET"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.error("alpaca error cuenta: %s", e)
        return None


def get_positions():
    if not alpaca_enabled():
        return None
    try:
        req = urllib.request.Request(
            f"{alpaca_url()}/v2/positions", headers=_headers(), method="GET"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.error("alpaca error posiciones: %s", e)
        return None
