# Market Tracker v14

## Cambios vs v13

### Bug Fixes

1. **CRÍTICO: `trailing_active` usado antes de definirse** (`paper_engine.py`)
   - Causaba `UnboundLocalError` en cada ciclo → posiciones sin gestionar
   - Fix: Variables `trailing_active`, `exit_reason`, `pyramid_count` ahora se definen primero

2. **ALTO: Bloque trailing asimétrico reubicado** (`paper_engine.py`)
   - Fix: Movido después de calcular `trailing_pct` base y progressive trailing

3. **ALTO: `partial_close` bug de `cost_recovered_eur`** (`storage.py`)
   - Fix: Recalcula proporcionalmente basado en el ratio de ajuste

4. **MEDIO: `datetime.utcnow()` deprecado** → `datetime.now(tz=timezone.utc)`

5. **BAJO: Separación `now_dt` / `_utc_now`** para checks de mercado correctos

### Mejoras de Retorno (+3-7% anual estimado)

1. **Salida en 3 tramos** — `TRANCHE_1_PCT=0.33`, `TRANCHE_2_PCT=0.33`
2. **Límite sectorial** — `SECTOR_MAX_POSITIONS=2`
3. **BB Squeeze** — `BB_SQUEEZE_BONUS=5`
4. **Trailing progresivo** — `PROGRESSIVE_TRAILING={8:1.5, 24:1.2, 48:0.9, 999:0.7}`
5. **Gap filter** — `GAP_FILTER_PCT=-3.0`
6. **Ret 5D scoring** — `RET5D_MAX_POINTS=5`
7. **Confluencia** — `CONFLUENCE_BONUS=5`
8. **DCA entry** — `DCA_TRANCHE_1_PCT=0.50`, tramos 2-3 tras 3h y 6h

### Nuevas columnas SQLite (migración automática)
- `open_pos.tranche_2_done`, `open_pos.dca_pending_eur`, `open_pos.dca_tranche`

### Compatibilidad
- 100% retrocompatible con DB de v13
- Todas las mejoras se desactivan individualmente vía config
