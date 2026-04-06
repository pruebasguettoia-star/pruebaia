"""
telegram_alerts.py — Alertas por Telegram.

Configurar en Railway:
  TELEGRAM_BOT_TOKEN = token de @BotFather
  TELEGRAM_CHAT_ID   = tu chat_id (de @userinfobot)
"""
import urllib.request
import json
import logging
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED

log = logging.getLogger("tracker")
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
                log.warning("telegram API error: %s", result)
        except Exception as e:
            _failures += 1
            if attempt < retry:
                import time
                time.sleep(3)
                log.warning("telegram intento %d falló: %s — reintentando", attempt+1, e)
            else:
                log.error("telegram falló tras %d intentos: %s", retry+1, e)
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


def alert_trade_open(ticker, name, score, price, currency, breakdown=None, trade_mode=None):
    """Notifica apertura de posición con los principales componentes del score."""
    if not TELEGRAM_ENABLED:
        return
    sym = "$" if currency == "USD" else "€"
    mode_label = {"momentum": " [MOM]", "rsi_divergence": " [DIV]",
                  "div_yield": " [DIV%]", "bonds": " [BOND]", "pyramid": " [PYRA]"}.get(trade_mode or "", "")
    msg = f"📈 <b>COMPRA{mode_label}</b> {name} ({ticker})\nScore: {score} · Precio: {sym}{price}"
    if breakdown and isinstance(breakdown, dict):
        parts = []
        for key in ("rsi", "bb", "rel"):
            note = breakdown.get(key, "")
            if note:
                short = note.split(" — ")[0] if " — " in note else note
                parts.append(short)
        if parts:
            msg += "\n<i>" + " · ".join(parts) + "</i>"
        regime = breakdown.get("regime")
        if regime:
            msg += f"\nRégimen: {regime}"
    send_message(msg)


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


def send_weekly_report(positions, capital, stats, vix, regime, top_tickers):
    """Informe semanal automático — se llama los lunes antes de apertura de mercado."""
    if not TELEGRAM_ENABLED:
        return
    tr    = stats.get("total_trades") or {}
    total = tr.get("total_trades", 0)
    wins  = tr.get("wins", 0)
    wr    = round(wins / max(total, 1) * 100)
    pnl   = tr.get("total_pnl_eur", 0)

    lines = [
        "📅 <b>Informe semanal — Market Tracker</b>",
        "",
        f"Capital libre: €{capital:,.0f}",
        f"Posiciones abiertas: {len(positions)}",
        f"VIX: {f'{vix:.1f}' if vix else '—'} · Régimen: {regime}",
        "",
        "<b>Histórico acumulado</b>",
        f"Trades: {total} · Win rate: {wr}% · P&L: €{pnl:+.0f}",
    ]
    if top_tickers:
        lines += ["", "<b>Top señales ahora</b>"]
        for row in top_tickers[:5]:
            lines.append(f"  {row['ticker']}: score {row.get('inv_score','—')} · RSI {row.get('rsi','—')}")

    send_message("\n".join(lines))


def get_failures():
    return _failures
