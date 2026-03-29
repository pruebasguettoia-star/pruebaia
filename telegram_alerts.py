"""
telegram_alerts.py — Alertas por Telegram.

Configurar en Railway:
  TELEGRAM_BOT_TOKEN = token de @BotFather
  TELEGRAM_CHAT_ID   = tu chat_id (de @userinfobot)

Envía alertas de:
  - Compra fuerte (strong_buy)
  - Trades ejecutados (compra/venta del paper trading)
"""
import urllib.request
import urllib.parse
import json
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED

_failures = 0


def send_message(text, parse_mode="HTML", retry=2):
    """Envía un mensaje por Telegram. Retorna True si OK."""
    global _failures
    if not TELEGRAM_ENABLED:
        return False
    for attempt in range(retry + 1):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = json.dumps({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }).encode()
            req = urllib.request.Request(url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            if result.get("ok"):
                _failures = 0
                return True
            else:
                print(f"[telegram] API error: {result}")
        except Exception as e:
            _failures += 1
            if attempt < retry:
                import time
                time.sleep(3)
                print(f"[telegram] intento {attempt+1} falló: {e} — reintentando")
            else:
                print(f"[telegram] falló tras {retry+1} intentos: {e}")
    return False


def alert_strong_buys(strong_buys):
    """Envía alerta de señales de compra fuerte."""
    if not strong_buys or not TELEGRAM_ENABLED:
        return
    lines = ["🟢 <b>COMPRA FUERTE</b>\n"]
    for r in strong_buys:
        lines.append(f"  <b>{r['name']}</b> ({r['ticker']})")
        lines.append(f"  RSI {r.get('rsi', '—')} · BB {r.get('bb_pct', '—')}% · Score {r.get('inv_score', '—')}")
        lines.append("")
    send_message("\n".join(lines))


def alert_trade_open(ticker, name, score, price, currency):
    """Notifica apertura de posición."""
    if not TELEGRAM_ENABLED:
        return
    sym = "$" if currency == "USD" else "€"
    send_message(
        f"📈 <b>COMPRA</b> {name} ({ticker})\n"
        f"Score: {score} · Precio: {sym}{price}"
    )


def alert_trade_close(ticker, name, ret_pct, pnl, reason, currency):
    """Notifica cierre de posición."""
    if not TELEGRAM_ENABLED:
        return
    sym = "$" if currency == "USD" else "€"
    emoji = "✅" if ret_pct >= 0 else "🔴"
    sign = "+" if ret_pct >= 0 else ""
    send_message(
        f"{emoji} <b>VENTA</b> {name} ({ticker})\n"
        f"Ret: {sign}{ret_pct:.2f}% · P&L: {sign}{sym}{abs(pnl):.0f}\n"
        f"Motivo: {reason}"
    )


def get_failures():
    return _failures
