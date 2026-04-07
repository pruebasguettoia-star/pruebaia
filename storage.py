"""
storage.py — Persistencia SQLite para paper trading.

Reemplaza paper2_trades.json. Ventajas:
  - Escritura atómica (SQLite WAL mode)
  - Queries sobre historial de trades
  - Resiste corrupción por kill de proceso
  - Compatible con Railway Volumes (/app/data/paper2.db)

Tablas:
  - state       → capital, config (1 fila)
  - open_pos    → posiciones abiertas
  - closed_pos  → historial de trades cerrados
  - equity_log  → snapshots de equity
  - cooldowns   → ticker + expiry
"""
import logging
import sqlite3
import json
import os
import threading
from datetime import datetime
log = logging.getLogger("tracker")

from config import PAPER2_INITIAL_CAP, PAPER_DATA_DIR

DB_PATH = os.path.join(PAPER_DATA_DIR, "paper2.db")
_local = threading.local()
_db_initialized = False


def _ensure_dir():
    """Crea el directorio de datos si no existe."""
    d = os.path.dirname(DB_PATH)
    if d and not os.path.exists(d):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


def _conn():
    """Devuelve una conexión thread-local. Reconecta si la conexión está rota."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")  # ping rápido para detectar conexión rota
        except sqlite3.OperationalError:
            log.warning("Conexión SQLite rota — reconectando")
            try:
                conn.close()
            except Exception:
                pass
            _local.conn = None
    if not hasattr(_local, "conn") or _local.conn is None:
        _ensure_dir()
        _local.conn = sqlite3.connect(DB_PATH, timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn.execute("PRAGMA cache_size=-8000")
        _local.conn.execute("PRAGMA busy_timeout=5000")
    return _local.conn


def init_db():
    """Crea las tablas si no existen.
    Usa execute() individual en vez de executescript() para evitar
    conflictos de transacción con SQLite WAL mode en Railway Volumes.
    """
    conn = _conn()
    # Cancelar cualquier transacción pendiente antes de crear tablas
    try:
        conn.rollback()
    except Exception:
        pass

    stmts = [
        """CREATE TABLE IF NOT EXISTS state (
            id INTEGER PRIMARY KEY CHECK(id=1),
            capital REAL NOT NULL DEFAULT 10000.0,
            alerted_json TEXT NOT NULL DEFAULT '[]'
        )""",
        "INSERT OR IGNORE INTO state (id, capital, alerted_json) VALUES (1, 10000.0, '[]')",
        """CREATE TABLE IF NOT EXISTS open_pos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT,
            entry_date TEXT NOT NULL,
            entry_price REAL NOT NULL,
            current_price REAL,
            peak_price REAL,
            currency TEXT DEFAULT 'EUR',
            shares REAL NOT NULL,
            ret_pct REAL DEFAULT 0,
            trailing_drop REAL DEFAULT 0,
            trailing_active INTEGER DEFAULT 0,
            trailing_pct REAL DEFAULT 2.0,
            hours_held REAL DEFAULT 0,
            hours_left REAL DEFAULT 24,
            entry_score INTEGER,
            score INTEGER,
            waiting_recovery INTEGER DEFAULT 0,
            trade_mode TEXT DEFAULT 'dip_buying',
            pyramid_count INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS closed_pos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT,
            entry_date TEXT NOT NULL,
            exit_date TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            peak_price REAL,
            currency TEXT DEFAULT 'EUR',
            shares REAL NOT NULL,
            ret_pct REAL,
            pnl REAL,
            pnl_eur REAL,
            reason TEXT,
            entry_score INTEGER,
            hours_held REAL,
            trailing_active INTEGER DEFAULT 0,
            trailing_pct REAL,
            is_partial INTEGER DEFAULT 0,
            trade_mode TEXT DEFAULT 'dip_buying'
        )""",
        """CREATE TABLE IF NOT EXISTS equity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            equity REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS cooldowns (
            ticker TEXT PRIMARY KEY,
            until_date TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_closed_exit_date ON closed_pos(exit_date)",
        "CREATE INDEX IF NOT EXISTS idx_closed_ticker    ON closed_pos(ticker)",
        "CREATE INDEX IF NOT EXISTS idx_closed_partial   ON closed_pos(is_partial)",
        "CREATE INDEX IF NOT EXISTS idx_open_ticker      ON open_pos(ticker)",
        "CREATE INDEX IF NOT EXISTS idx_cooldowns_until  ON cooldowns(until_date)",
        # Watchlist: tickers que estuvieron cerca del umbral (score 75-84)
        # Permite detectar cuándo cruzan al umbral real sin esperar el ciclo de refresh
        """CREATE TABLE IF NOT EXISTS watchlist (
            ticker TEXT PRIMARY KEY,
            name TEXT,
            score INTEGER,
            signal TEXT,
            rsi REAL,
            bb_pct REAL,
            last_seen TEXT NOT NULL,
            times_seen INTEGER DEFAULT 1
        )""",
        "CREATE INDEX IF NOT EXISTS idx_watchlist_score ON watchlist(score DESC)",
    ]
    for stmt in stmts:
        try:
            conn.execute(stmt)
        except Exception as e:
            log.warning("init_db stmt ignorado: %s", e)
    conn.commit()

    # ── Migraciones — añadir columnas nuevas si la DB ya existía sin ellas ──────
    _migrations = [
        # open_pos
        ("open_pos",   "peak_price",       "REAL"),
        ("open_pos",   "trailing_drop",    "REAL DEFAULT 0"),
        ("open_pos",   "trailing_active",  "INTEGER DEFAULT 0"),
        ("open_pos",   "trailing_pct",     "REAL DEFAULT 2.0"),
        ("open_pos",   "waiting_recovery", "INTEGER DEFAULT 0"),
        ("open_pos",   "trade_mode",       "TEXT DEFAULT 'dip_buying'"),
        ("open_pos",   "pyramid_count",    "INTEGER DEFAULT 0"),
        ("open_pos",   "entry_score",      "INTEGER"),
        ("open_pos",   "score",            "INTEGER"),
        # closed_pos
        ("closed_pos", "peak_price",       "REAL"),
        ("closed_pos", "is_partial",       "INTEGER DEFAULT 0"),
        ("closed_pos", "trade_mode",       "TEXT DEFAULT 'dip_buying'"),
        ("closed_pos", "trailing_active",  "INTEGER DEFAULT 0"),
        ("closed_pos", "trailing_pct",     "REAL"),
        ("closed_pos", "pnl_eur",          "REAL"),
        ("closed_pos", "hours_held",       "REAL"),
        ("closed_pos", "entry_score",      "INTEGER"),
        # state
        ("state",      "alerted_json",     "TEXT NOT NULL DEFAULT '[]'"),
    ]
    for table, col, col_def in _migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
            conn.commit()
            log.info("migración: columna %s.%s añadida", table, col)
        except Exception:
            pass  # columna ya existe — ignorar
    log.info("SQLite inicializado: %s", DB_PATH)


# ── STATE (capital) ───────────────────────────────────────────────────────────
def get_capital():
    row = _conn().execute("SELECT capital FROM state WHERE id=1").fetchone()
    return float(row["capital"]) if row else PAPER2_INITIAL_CAP


def set_capital(amount):
    _conn().execute("UPDATE state SET capital=? WHERE id=1", (round(amount, 6),))
    _conn().commit()


def add_capital(amount):
    """Suma amount al capital actual (atómico)."""
    _conn().execute("UPDATE state SET capital = capital + ? WHERE id=1", (round(amount, 6),))
    _conn().commit()


def sub_capital(amount):
    _conn().execute("UPDATE state SET capital = capital - ? WHERE id=1", (round(amount, 6),))
    _conn().commit()


def get_alerted() -> set:
    """Carga el set de tickers en strong_buy desde la última sesión."""
    row = _conn().execute("SELECT alerted_json FROM state WHERE id=1").fetchone()
    if not row:
        return set()
    try:
        return set(json.loads(row["alerted_json"]))
    except Exception:
        return set()


def save_alerted(alerted: set):
    """Persiste el set alerted en SQLite para sobrevivir reinicios."""
    _conn().execute(
        "UPDATE state SET alerted_json=? WHERE id=1",
        (json.dumps(sorted(alerted)),)
    )
    _conn().commit()


# ── OPEN POSITIONS ────────────────────────────────────────────────────────────
def get_open_positions():
    rows = _conn().execute("SELECT * FROM open_pos ORDER BY entry_date").fetchall()
    return [_row_to_dict(r) for r in rows]

# Alias para compatibilidad con migrate_from_json y código legado
def add_open_position(pos):
    """Alias de atomic_open_position sin deducir capital — solo para migración JSON."""
    cost_eur = pos.get("shares", 0) * pos.get("entry_price", 0)
    try:
        atomic_open_position(pos, cost_eur)
    except Exception as e:
        log.warning("add_open_position fallback: %s", e)


def atomic_open_position(pos, cost_eur):
    """Resta capital y añade posición en una sola transacción — evita estado corrupto."""
    conn = _conn()
    conn.execute("UPDATE state SET capital = capital - ? WHERE id=1", (round(cost_eur, 6),))
    conn.execute("""
        INSERT INTO open_pos (ticker, name, entry_date, entry_price, current_price,
            peak_price, currency, shares, ret_pct, trailing_drop, trailing_active,
            trailing_pct, hours_held, hours_left, entry_score, score, waiting_recovery,
            trade_mode, pyramid_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        pos["ticker"], pos.get("name"), pos["entry_date"], pos["entry_price"],
        pos.get("current_price", pos["entry_price"]), pos.get("peak_price", pos["entry_price"]),
        pos.get("currency", "EUR"), pos["shares"], pos.get("ret_pct", 0),
        pos.get("trailing_drop", 0), int(pos.get("trailing_active", False)),
        pos.get("trailing_pct", 2.0), pos.get("hours_held", 0),
        pos.get("hours_left", 24), pos.get("entry_score"), pos.get("score"),
        int(pos.get("waiting_recovery", False)),
        pos.get("trade_mode", "dip_buying"),
        int(pos.get("pyramid_count", 0)),
    ))
    conn.commit()


def partial_close(ticker, shares_sold, cost_recovered_eur):
    """Reduce las shares de una posición abierta (salida parcial) y devuelve capital."""
    if shares_sold <= 0:
        log.warning("partial_close: shares_sold=%.6f inválido para %s", shares_sold, ticker)
        return
    conn = _conn()
    row = conn.execute("SELECT shares FROM open_pos WHERE ticker=?", (ticker,)).fetchone()
    if row is None:
        log.warning("partial_close: posición %s no encontrada", ticker)
        return
    if float(row["shares"]) < shares_sold - 1e-9:
        shares_sold = float(row["shares"])
        cost_recovered_eur = shares_sold
        log.warning("partial_close: ajustando shares_sold a %.6f para %s", shares_sold, ticker)
    conn.execute("UPDATE state SET capital = capital + ? WHERE id=1", (round(cost_recovered_eur, 6),))
    conn.execute(
        "UPDATE open_pos SET shares = shares - ? WHERE ticker = ?",
        (round(shares_sold, 6), ticker)
    )
    conn.commit()


def atomic_pyramid(ticker, extra_shares, native_cost):
    """Añade shares a una posición existente (pyramiding) y resta capital."""
    conn = _conn()
    row = conn.execute(
        "SELECT shares, entry_price FROM open_pos WHERE ticker=?", (ticker,)
    ).fetchone()
    if row is None:
        log.warning("atomic_pyramid: posición %s no encontrada", ticker)
        return
    orig_shares = float(row["shares"])
    orig_entry  = float(row["entry_price"])
    extra_entry = native_cost / extra_shares if extra_shares > 0 else orig_entry
    total_shares = orig_shares + extra_shares
    avg_entry = (orig_shares * orig_entry + extra_shares * extra_entry) / total_shares if total_shares > 0 else orig_entry
    conn.execute("""
        UPDATE open_pos
        SET shares = shares + ?,
            pyramid_count = pyramid_count + 1,
            entry_price = ?
        WHERE ticker = ?
    """, (round(extra_shares, 6), round(avg_entry, 6), ticker))
    conn.commit()


def update_open_position(ticker, updates):
    """Actualiza campos de una posición abierta por ticker."""
    if not updates:
        return
    cols = ", ".join(f"{k}=?" for k in updates.keys())
    vals = list(updates.values())
    vals.append(ticker)
    _conn().execute(f"UPDATE open_pos SET {cols} WHERE ticker=?", vals)
    _conn().commit()


def remove_open_position(ticker):
    _conn().execute("DELETE FROM open_pos WHERE ticker=?", (ticker,))
    _conn().commit()


def get_open_tickers():
    rows = _conn().execute("SELECT ticker FROM open_pos").fetchall()
    return {r["ticker"] for r in rows}


# ── CLOSED POSITIONS ──────────────────────────────────────────────────────────
def add_closed_trade(trade):
    _conn().execute("""
        INSERT INTO closed_pos (ticker, name, entry_date, exit_date, entry_price,
            exit_price, peak_price, currency, shares, ret_pct, pnl, pnl_eur,
            reason, entry_score, hours_held, trailing_active, trailing_pct,
            is_partial, trade_mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade["ticker"], trade.get("name"), trade["entry_date"], trade["exit_date"],
        trade["entry_price"], trade["exit_price"], trade.get("peak_price"),
        trade.get("currency", "EUR"), trade["shares"], trade.get("ret_pct"),
        trade.get("pnl"), trade.get("pnl_eur"), trade.get("reason"),
        trade.get("entry_score"), trade.get("hours_held"),
        int(trade.get("trailing_active", False)), trade.get("trailing_pct"),
        int(trade.get("is_partial", False)),
        trade.get("trade_mode", "dip_buying"),
    ))
    _conn().commit()


def get_closed_trades(limit=200):
    rows = _conn().execute(
        "SELECT * FROM closed_pos ORDER BY exit_date DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_trades_by_period(start_date, end_date=None):
    """Trades cerrados en un periodo. Fechas en formato 'YYYY-MM-DD'."""
    if end_date:
        rows = _conn().execute(
            "SELECT * FROM closed_pos WHERE exit_date >= ? AND exit_date <= ? ORDER BY exit_date",
            (start_date, end_date)
        ).fetchall()
    else:
        rows = _conn().execute(
            "SELECT * FROM closed_pos WHERE exit_date >= ? ORDER BY exit_date",
            (start_date,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_performance_stats():
    """Estadísticas de rendimiento globales y por periodo."""
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) as n FROM closed_pos").fetchone()["n"]
    if total == 0:
        return {"total_trades": 0}

    stats = conn.execute("""
        SELECT
            COUNT(*) as total_trades,
            SUM(CASE WHEN ret_pct > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN ret_pct <= 0 THEN 1 ELSE 0 END) as losses,
            ROUND(AVG(ret_pct), 2) as avg_ret,
            ROUND(AVG(CASE WHEN ret_pct > 0 THEN ret_pct END), 2) as avg_win,
            ROUND(AVG(CASE WHEN ret_pct <= 0 THEN ret_pct END), 2) as avg_loss,
            ROUND(MAX(ret_pct), 2) as best_trade,
            ROUND(MIN(ret_pct), 2) as worst_trade,
            ROUND(SUM(pnl_eur), 2) as total_pnl_eur,
            ROUND(AVG(hours_held), 1) as avg_hours
        FROM closed_pos
        WHERE COALESCE(is_partial, 0) = 0
    """).fetchone()

    partial_count = conn.execute(
        "SELECT COUNT(*) as n, ROUND(SUM(pnl_eur),2) as pnl FROM closed_pos WHERE COALESCE(is_partial,0)=1"
    ).fetchone()

    monthly = conn.execute("""
        SELECT
            SUBSTR(exit_date, 1, 7) as month,
            SUM(CASE WHEN COALESCE(is_partial,0)=0 THEN 1 ELSE 0 END) as trades,
            ROUND(SUM(pnl_eur), 2) as pnl_eur,
            ROUND(AVG(CASE WHEN COALESCE(is_partial,0)=0 THEN ret_pct END), 2) as avg_ret,
            SUM(CASE WHEN COALESCE(is_partial,0)=0 AND ret_pct > 0 THEN 1 ELSE 0 END) as wins
        FROM closed_pos
        WHERE exit_date != 'OPEN'
        GROUP BY SUBSTR(exit_date, 1, 7)
        ORDER BY month
    """).fetchall()

    weekly = conn.execute("""
        SELECT
            SUBSTR(exit_date, 1, 10) as week_start,
            SUM(CASE WHEN COALESCE(is_partial,0)=0 THEN 1 ELSE 0 END) as trades,
            ROUND(SUM(pnl_eur), 2) as pnl_eur,
            ROUND(AVG(CASE WHEN COALESCE(is_partial,0)=0 THEN ret_pct END), 2) as avg_ret
        FROM closed_pos
        WHERE exit_date != 'OPEN'
        GROUP BY CAST(JULIANDAY(exit_date) / 7 AS INTEGER)
        ORDER BY week_start
    """).fetchall()

    reasons = conn.execute("""
        SELECT reason, COUNT(*) as n, ROUND(AVG(ret_pct), 2) as avg_ret
        FROM closed_pos
        WHERE COALESCE(is_partial, 0) = 0
        GROUP BY reason ORDER BY n DESC LIMIT 10
    """).fetchall()

    result = _row_to_dict(stats)
    result["partial_trades"]  = partial_count["n"]
    result["partial_pnl_eur"] = partial_count["pnl"]

    return {
        "total_trades":  result,
        "monthly":       [_row_to_dict(r) for r in monthly],
        "weekly":        [_row_to_dict(r) for r in weekly],
        "exit_reasons":  [_row_to_dict(r) for r in reasons],
    }


# ── EQUITY LOG ────────────────────────────────────────────────────────────────
def add_equity_snapshot(equity):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    last = _conn().execute(
        "SELECT date FROM equity_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last:
        try:
            last_dt = datetime.strptime(last["date"], "%Y-%m-%d %H:%M")
            if (datetime.now() - last_dt).total_seconds() < 3600:
                return
        except Exception:
            pass
    _conn().execute("INSERT INTO equity_log (date, equity) VALUES (?, ?)", (now_str, round(equity, 2)))
    _conn().execute("""
        DELETE FROM equity_log WHERE id NOT IN (
            SELECT id FROM equity_log ORDER BY id DESC LIMIT 2000
        )
    """)
    _conn().commit()


def get_equity_log(limit=500):
    rows = _conn().execute(
        "SELECT date, equity FROM equity_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [{"date": r["date"], "equity": r["equity"]} for r in reversed(rows)]


# ── COOLDOWNS ─────────────────────────────────────────────────────────────────
def set_cooldown(ticker, until_str):
    _conn().execute(
        "INSERT OR REPLACE INTO cooldowns (ticker, until_date) VALUES (?, ?)",
        (ticker, until_str)
    )
    _conn().commit()


def get_cooldowns():
    rows = _conn().execute("SELECT ticker, until_date FROM cooldowns").fetchall()
    return {r["ticker"]: r["until_date"] for r in rows}


def clean_expired_cooldowns():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    _conn().execute("DELETE FROM cooldowns WHERE until_date <= ?", (now_str,))
    _conn().commit()


def get_cooldowns_trailing():
    """Alias de get_cooldowns() con nombre más preciso."""
    rows = _conn().execute("SELECT ticker, until_date FROM cooldowns").fetchall()
    return {r["ticker"]: r["until_date"] for r in rows}


def remove_cooldown(ticker):
    """Elimina el cooldown de un ticker (liberación anticipada)."""
    _conn().execute("DELETE FROM cooldowns WHERE ticker=?", (ticker,))
    _conn().commit()


# ── RESET ─────────────────────────────────────────────────────────────────────
def reset_all():
    conn = _conn()
    try:
        conn.rollback()
    except Exception:
        pass
    for stmt in [
        "DELETE FROM open_pos",
        "DELETE FROM closed_pos",
        "DELETE FROM equity_log",
        "DELETE FROM cooldowns",
        "UPDATE state SET capital = 10000.0 WHERE id=1",
    ]:
        try:
            conn.execute(stmt)
        except Exception as e:
            log.warning("reset stmt ignorado: %s", e)
    conn.commit()
    log.info("reset completo")


# ── EXPORT CSV ────────────────────────────────────────────────────────────────
def export_trades_csv():
    """Exporta trades cerrados como CSV string."""
    rows = _conn().execute(
        "SELECT * FROM closed_pos ORDER BY exit_date"
    ).fetchall()
    if not rows:
        return "No trades\n"
    cols = list(rows[0].keys())
    lines = [",".join(cols)]
    for r in rows:
        lines.append(",".join(str(r[c]) if r[c] is not None else "0" if c == "is_partial" else "" for c in cols))
    return "\n".join(lines)


# ── MIGRATE FROM JSON ─────────────────────────────────────────────────────────
def migrate_from_json(json_path):
    """Importa datos de paper2_trades.json existente a SQLite."""
    if not os.path.exists(json_path):
        log.info("no hay JSON para migrar")
        return False
    try:
        with open(json_path) as f:
            data = json.load(f)
        conn = _conn()
        conn.execute("UPDATE state SET capital=? WHERE id=1", (data.get("capital", PAPER2_INITIAL_CAP),))
        for pos in data.get("open", []):
            pos.setdefault("pyramid_count", 0)
            add_open_position(pos)
        for trade in data.get("closed", []):
            if "pnl_eur" not in trade:
                trade["pnl_eur"] = trade.get("pnl")
            add_closed_trade(trade)
        for entry in data.get("equity_log", [])[-500:]:
            conn.execute("INSERT INTO equity_log (date, equity) VALUES (?, ?)",
                         (entry["date"], entry["equity"]))
        for ticker, until in data.get("cooldowns", {}).items():
            set_cooldown(ticker, until)
        conn.commit()
        n_open = len(data.get("open", []))
        n_closed = len(data.get("closed", []))
        log.info("migrado JSON → SQLite: %d abiertas, %d cerradas", n_open, n_closed)
        os.rename(json_path, json_path + ".migrated")
        return True
    except Exception as e:
        log.error("error migrando JSON: %s", e)
        return False


# ── HELPERS ───────────────────────────────────────────────────────────────────
def _row_to_dict(row):
    if row is None:
        return {}
    d = dict(row)
    for k in ("trailing_active", "waiting_recovery"):
        if k in d and isinstance(d[k], int):
            d[k] = bool(d[k])
    return d


# ── WATCHLIST ─────────────────────────────────────────────────────────────────
def upsert_watchlist(ticker, name, score, signal, rsi, bb_pct):
    """Inserta o actualiza un ticker en la watchlist (score entre 70-84)."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    _conn().execute("""
        INSERT INTO watchlist (ticker, name, score, signal, rsi, bb_pct, last_seen, times_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(ticker) DO UPDATE SET
            score      = excluded.score,
            signal     = excluded.signal,
            rsi        = excluded.rsi,
            bb_pct     = excluded.bb_pct,
            last_seen  = excluded.last_seen,
            times_seen = watchlist.times_seen + 1
    """, (ticker, name, score, signal, rsi, bb_pct, now_str))
    _conn().commit()


def get_watchlist(limit=20):
    """Devuelve los tickers más cercanos al umbral, ordenados por score desc."""
    rows = _conn().execute(
        "SELECT * FROM watchlist ORDER BY score DESC, times_seen DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def clean_watchlist_stale(hours=48):
    """Elimina entradas no actualizadas en las últimas N horas."""
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    _conn().execute("DELETE FROM watchlist WHERE last_seen < ?", (cutoff,))
    _conn().commit()


# ── NO AUTO-INIT — llamar init_db() desde paper_engine.init() ────────────────
