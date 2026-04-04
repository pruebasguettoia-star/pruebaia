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
import sqlite3
import json
import os
import threading
from datetime import datetime
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
    """Devuelve una conexión thread-local."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _ensure_dir()
        _local.conn = sqlite3.connect(DB_PATH, timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")   # más rápido en Railway Volume; WAL garantiza durabilidad
        _local.conn.execute("PRAGMA cache_size=-8000")     # 8 MB de cache en memoria
        _local.conn.execute("PRAGMA busy_timeout=5000")
    return _local.conn


def init_db():
    """Crea las tablas si no existen."""
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS state (
            id INTEGER PRIMARY KEY CHECK(id=1),
            capital REAL NOT NULL DEFAULT 10000.0
        );
        INSERT OR IGNORE INTO state (id, capital) VALUES (1, 10000.0);

        CREATE TABLE IF NOT EXISTS open_pos (
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
        );

        CREATE TABLE IF NOT EXISTS closed_pos (
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
            is_partial INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS equity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            equity REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cooldowns (
            ticker TEXT PRIMARY KEY,
            until_date TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_closed_exit_date ON closed_pos(exit_date);
        CREATE INDEX IF NOT EXISTS idx_closed_ticker    ON closed_pos(ticker);
        CREATE INDEX IF NOT EXISTS idx_closed_partial   ON closed_pos(is_partial);
        CREATE INDEX IF NOT EXISTS idx_open_ticker      ON open_pos(ticker);
        CREATE INDEX IF NOT EXISTS idx_cooldowns_until  ON cooldowns(until_date);
    """)
    conn.commit()
    # Migración: añadir columna trade_mode si la DB ya existía sin ella
    try:
        conn.execute("ALTER TABLE open_pos ADD COLUMN trade_mode TEXT DEFAULT 'dip_buying'")
        conn.commit()
        print("[storage] columna trade_mode añadida a open_pos")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE open_pos ADD COLUMN pyramid_count INTEGER DEFAULT 0")
        conn.commit()
        print("[storage] columna pyramid_count añadida a open_pos")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE closed_pos ADD COLUMN is_partial INTEGER DEFAULT 0")
        conn.commit()
        print("[storage] columna is_partial añadida a closed_pos")
    except Exception:
        pass
    print(f"[storage] SQLite inicializado: {DB_PATH}")


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


# ── OPEN POSITIONS ────────────────────────────────────────────────────────────
def get_open_positions():
    rows = _conn().execute("SELECT * FROM open_pos ORDER BY entry_date").fetchall()
    return [_row_to_dict(r) for r in rows]


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



    _conn().execute("""
        INSERT INTO open_pos (ticker, name, entry_date, entry_price, current_price,
            peak_price, currency, shares, ret_pct, trailing_drop, trailing_active,
            trailing_pct, hours_held, hours_left, entry_score, score, waiting_recovery,
            trade_mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        pos["ticker"], pos.get("name"), pos["entry_date"], pos["entry_price"],
        pos.get("current_price", pos["entry_price"]), pos.get("peak_price", pos["entry_price"]),
        pos.get("currency", "EUR"), pos["shares"], pos.get("ret_pct", 0),
        pos.get("trailing_drop", 0), int(pos.get("trailing_active", False)),
        pos.get("trailing_pct", 2.0), pos.get("hours_held", 0),
        pos.get("hours_left", 24), pos.get("entry_score"), pos.get("score"),
        int(pos.get("waiting_recovery", False)),
        pos.get("trade_mode", "dip_buying"),
    ))
    _conn().commit()


def partial_close(ticker, shares_sold, cost_recovered_eur):
    """Reduce las shares de una posición abierta (salida parcial) y devuelve capital.
    Transacción atómica — evita estado inconsistente.
    """
    if shares_sold <= 0:
        print(f"[storage] partial_close: shares_sold={shares_sold} inválido para {ticker}")
        return
    conn = _conn()
    # Verificar que quedan suficientes shares antes de reducir
    row = conn.execute("SELECT shares FROM open_pos WHERE ticker=?", (ticker,)).fetchone()
    if row is None:
        print(f"[storage] partial_close: posición {ticker} no encontrada")
        return
    if float(row["shares"]) < shares_sold - 1e-9:
        # Seguridad: no dejar shares negativas — vender solo lo que hay
        shares_sold = float(row["shares"])
        cost_recovered_eur = shares_sold  # valor mínimo aproximado, no debería ocurrir
        print(f"[storage] partial_close: ajustando shares_sold a {shares_sold:.6f} para {ticker}")
    conn.execute("UPDATE state SET capital = capital + ? WHERE id=1", (round(cost_recovered_eur, 6),))
    conn.execute(
        "UPDATE open_pos SET shares = shares - ? WHERE ticker = ?",
        (round(shares_sold, 6), ticker)
    )
    conn.commit()


def atomic_pyramid(ticker, extra_shares, cost_eur):
    """Añade shares a una posición existente (pyramiding) y resta capital.
    Todo en una sola transacción para evitar estado inconsistente.
    """
    conn = _conn()
    conn.execute("UPDATE state SET capital = capital - ? WHERE id=1", (round(cost_eur, 6),))
    conn.execute("""
        UPDATE open_pos
        SET shares = shares + ?,
            pyramid_count = pyramid_count + 1
        WHERE ticker = ?
    """, (round(extra_shares, 6), ticker))
    conn.commit()



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
            reason, entry_score, hours_held, trailing_active, trailing_pct, is_partial)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade["ticker"], trade.get("name"), trade["entry_date"], trade["exit_date"],
        trade["entry_price"], trade["exit_price"], trade.get("peak_price"),
        trade.get("currency", "EUR"), trade["shares"], trade.get("ret_pct"),
        trade.get("pnl"), trade.get("pnl_eur"), trade.get("reason"),
        trade.get("entry_score"), trade.get("hours_held"),
        int(trade.get("trailing_active", False)), trade.get("trailing_pct"),
        int(trade.get("is_partial", False)),
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
    """Estadísticas de rendimiento globales y por periodo.
    Las salidas parciales (is_partial=1) se excluyen de win_rate y avg_ret
    porque siempre son positivas (se activan al llegar al take_profit),
    lo que sesgaría artificialmente las métricas.
    """
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) as n FROM closed_pos").fetchone()["n"]
    if total == 0:
        return {"total_trades": 0}

    # Stats sobre trades completos únicamente (excluye parciales)
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

    # Contar parciales por separado para info
    partial_count = conn.execute(
        "SELECT COUNT(*) as n, ROUND(SUM(pnl_eur),2) as pnl FROM closed_pos WHERE COALESCE(is_partial,0)=1"
    ).fetchone()

    # Rendimiento por mes — incluye parciales en PnL (dinero real) pero no en trade count
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

    # Rendimiento por semana
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

    # Top razones de salida (excluye parciales — tendrían su propia razón uniforme)
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
    # Throttle: solo un snapshot por hora para reducir escrituras en Railway
    last = _conn().execute(
        "SELECT date FROM equity_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last:
        try:
            last_dt = datetime.strptime(last["date"], "%Y-%m-%d %H:%M")
            if (datetime.now() - last_dt).total_seconds() < 3600:
                return  # Ya hay un snapshot reciente — omitir
        except Exception:
            pass
    _conn().execute("INSERT INTO equity_log (date, equity) VALUES (?, ?)", (now_str, round(equity, 2)))
    # Mantener solo últimos 2000 registros
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


# ── RESET ─────────────────────────────────────────────────────────────────────
def reset_all():
    conn = _conn()
    conn.executescript("""
        DELETE FROM open_pos;
        DELETE FROM closed_pos;
        DELETE FROM equity_log;
        DELETE FROM cooldowns;
        UPDATE state SET capital = 10000.0 WHERE id=1;
    """)
    conn.commit()
    print("[storage] reset completo")


# ── EXPORT CSV ────────────────────────────────────────────────────────────────
def export_trades_csv():
    """Exporta trades cerrados como CSV string."""
    rows = _conn().execute(
        "SELECT * FROM closed_pos ORDER BY exit_date"
    ).fetchall()
    if not rows:
        return "No trades\n"

    cols = rows[0].keys()
    lines = [",".join(cols)]
    for r in rows:
        lines.append(",".join(str(r[c]) if r[c] is not None else "" for c in cols))
    return "\n".join(lines)


# ── MIGRATE FROM JSON ─────────────────────────────────────────────────────────
def migrate_from_json(json_path):
    """Importa datos de paper2_trades.json existente a SQLite."""
    if not os.path.exists(json_path):
        print("[storage] no hay JSON para migrar")
        return False
    try:
        with open(json_path) as f:
            data = json.load(f)
        conn = _conn()

        # Capital
        conn.execute("UPDATE state SET capital=? WHERE id=1", (data.get("capital", PAPER2_INITIAL_CAP),))

        # Open positions
        for pos in data.get("open", []):
            pos.setdefault("pyramid_count", 0)   # evita TypeError en engine al leer NULL
            add_open_position(pos)

        # Closed trades
        for trade in data.get("closed", []):
            if "pnl_eur" not in trade:
                trade["pnl_eur"] = trade.get("pnl")  # fallback
            add_closed_trade(trade)

        # Equity log
        for entry in data.get("equity_log", [])[-500:]:
            conn.execute("INSERT INTO equity_log (date, equity) VALUES (?, ?)",
                         (entry["date"], entry["equity"]))

        # Cooldowns
        for ticker, until in data.get("cooldowns", {}).items():
            set_cooldown(ticker, until)

        conn.commit()
        n_open = len(data.get("open", []))
        n_closed = len(data.get("closed", []))
        print(f"[storage] migrado JSON → SQLite: {n_open} abiertas, {n_closed} cerradas")

        # Renombrar JSON para evitar re-migración
        os.rename(json_path, json_path + ".migrated")
        return True
    except Exception as e:
        print(f"[storage] error migrando JSON: {e}")
        return False


# ── HELPERS ───────────────────────────────────────────────────────────────────
def _row_to_dict(row):
    if row is None:
        return {}
    d = dict(row)
    # Convertir booleanos de SQLite (0/1) a Python
    for k in ("trailing_active", "waiting_recovery"):
        if k in d and isinstance(d[k], int):
            d[k] = bool(d[k])
    return d


# ── NO AUTO-INIT — llamar init_db() desde paper_engine.init() ────────────────
