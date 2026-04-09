"""
fix_capital.py — Corrección puntual del capital en SQLite.

Sube este archivo a Railway junto al resto del código.
Se ejecuta UNA SOLA VEZ al arrancar y luego se desactiva solo.
"""
import os
import sqlite3
import logging

log = logging.getLogger("tracker")

CORRECT_CAPITAL = 9414.46
FLAG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".capital_fixed")


def run():
    # Si ya se ejecutó antes, no hacer nada
    if os.path.exists(FLAG_FILE):
        return

    try:
        from config import PAPER_DATA_DIR
        db_path = os.path.join(PAPER_DATA_DIR, "paper2.db")

        if not os.path.exists(db_path):
            log.warning("fix_capital: no se encontró paper2.db en %s", db_path)
            return

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT capital FROM state WHERE id=1").fetchone()
        if row:
            old_val = row[0]
            conn.execute("UPDATE state SET capital=? WHERE id=1", (CORRECT_CAPITAL,))
            conn.commit()
            log.info("fix_capital: capital corregido %.2f → %.2f", old_val, CORRECT_CAPITAL)
        conn.close()

        # Marcar como ejecutado para no volver a correr
        with open(FLAG_FILE, "w") as f:
            f.write("done")

    except Exception as e:
        log.error("fix_capital error: %s", e)
