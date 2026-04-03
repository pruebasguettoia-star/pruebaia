"""
alpaca_api.py — Integración con Alpaca para órdenes de paper/live trading.
"""
import json
import urllib.request
from config import alpaca_enabled, alpaca_key, alpaca_secret, alpaca_url, ALPACA_TRADEABLE


def _headers():
    return {
        "APCA-API-KEY-ID":     alpaca_key(),
        "APCA-API-SECRET-KEY": alpaca_secret(),
        "Content-Type":        "application/json",
    }


def place_order(ticker, side, notional_usd):
    """Envía orden de mercado. side='buy'|'sell'. Devuelve order_id o None."""
    if not alpaca_enabled():
        print("[alpaca] desactivado — configura ALPACA_API_KEY y ALPACA_SECRET_KEY")
        return None
    if ticker not in ALPACA_TRADEABLE:
        print(f"[alpaca] {ticker} no operable en Alpaca — omitiendo")
        return None
    try:
        payload = {"symbol": ticker, "side": side, "type": "market", "time_in_force": "gtc"}
        if side == "buy":
            existing = get_position_qty(ticker)
            if existing is not None and float(existing) > 0:
                print(f"[alpaca] {ticker} ya tiene posición ({existing} shares) — omitiendo")
                return None
            payload["notional"] = str(round(notional_usd, 2))
        else:
            qty = get_position_qty(ticker)
            if qty is None or float(qty) <= 0:
                print(f"[alpaca] no hay posición para {ticker}")
                return None
            payload["qty"] = qty

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{alpaca_url()}/v2/orders", data=data, headers=_headers(), method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        oid = result.get("id")
        print(f"[alpaca] {side} {ticker} — id:{oid} status:{result.get('status')}")
        return oid
    except Exception as e:
        print(f"[alpaca] error {side} {ticker}: {e}")
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
        print(f"[alpaca] error cuenta: {e}")
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
        print(f"[alpaca] error posiciones: {e}")
        return None
