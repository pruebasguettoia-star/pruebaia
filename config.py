"""
config.py — Configuración centralizada del Market Tracker.
Todas las constantes, tickers, y settings de Railway van aquí.
"""
import os

# ── SEGURIDAD ─────────────────────────────────────────────────────────────────
# Token para endpoints sensibles. En Railway: ADMIN_TOKEN = <tu_token_secreto>
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# ── ALPACA ────────────────────────────────────────────────────────────────────
def alpaca_enabled():
    return bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))
def alpaca_key():    return os.environ.get("ALPACA_API_KEY", "")
def alpaca_secret(): return os.environ.get("ALPACA_SECRET_KEY", "")
def alpaca_url():    return os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# ── EMAIL ─────────────────────────────────────────────────────────────────────
# ── TELEGRAM ──────────────────────────────────────────────────────────────────
# En Railway: TELEGRAM_BOT_TOKEN = tu_token, TELEGRAM_CHAT_ID = tu_chat_id
# Para obtenerlos:
#   1. Habla con @BotFather en Telegram → /newbot → copia el token
#   2. Habla con @userinfobot → copia tu chat_id
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)

# ── PAPER TRADING ─────────────────────────────────────────────────────────────
def _get_data_dir():
    """Devuelve un directorio escribible para datos persistentes."""
    d = os.environ.get("PAPER_DATA_DIR", "")
    if d and os.path.isdir(d):
        return d
    # Fallback: intentar directorio del script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    test_file = os.path.join(script_dir, ".write_test")
    try:
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        return script_dir
    except Exception:
        pass
    # Último recurso: /tmp (no persiste entre deploys pero no crashea)
    return "/tmp"

PAPER_DATA_DIR      = _get_data_dir()
PAPER2_FILE         = os.path.join(PAPER_DATA_DIR, "paper2_trades.json")
PAPER2_INITIAL_CAP  = 10_000.0
PAPER2_POSITION_PCT = 0.10
PAPER2_MIN_SCORE    = 85
PAPER2_HOLD_HOURS   = 24
PAPER2_STOP_LOSS    = -7.0
PAPER2_TAKE_PROFIT  = 5.0
PAPER2_TRAILING_MIN = 3.0
PAPER2_MAX_HOURS    = 48.0

# ── TRAILING DINÁMICO POR RÉGIMEN ─────────────────────────────────────────────
# En bull tranquilo (BULL_QUIET) activamos el trailing más tarde para dejar
# correr tendencias. En pánico lo activamos antes para proteger ganancias.
TAKE_PROFIT_BY_REGIME = {
    "BULL_QUIET":    10.0,  # activa trailing en +10% — deja correr tendencias
    "BULL_VOLATILE": 5.0,   # igual que antes
    "BEAR_MODERATE": 4.0,   # más conservador
    "BEAR_PANIC":    3.0,   # protección agresiva
}

# ── STOP DINÁMICO POR ATR ────────────────────────────────────────────────────
# Stop = ATR_STOP_MULT × ATR%, entre STOP_MIN y STOP_MAX
# Reemplaza el stop fijo de -7% para todos los activos
ATR_STOP_MULT = 2.5        # multiplicador ATR para stop
ATR_STOP_MIN  = 5.0        # stop mínimo (activos muy estables: SHY, AGG)
ATR_STOP_MAX  = 10.0       # stop máximo (activos muy volátiles: COIN, ARKK)

# ── EXTENSIÓN DE HOLD POR SCORE ──────────────────────────────────────────────
# Si a las HOLD_HOURS el score sube a ≥HOLD_EXT_SCORE y ret>HOLD_EXT_RET_PCT,
# extender el hold máximo a HOLD_EXT_MAX_HOURS (en vez de MAX_HOURS)
HOLD_EXT_SCORE     = 85     # score mínimo para extensión (= umbral de entrada)
HOLD_EXT_RET_PCT   = 2.0   # ret mínimo en % para extensión
HOLD_EXT_MAX_HOURS = 84.0  # hold máximo extendido (>MOM_MAX_HOURS=72 para añadir valor)

# ── PYRAMIDING ───────────────────────────────────────────────────────────────
# Si una posición lleva ≥PYRA_MIN_RET% y ≥PYRA_MIN_HOURS,
# y el score sigue ≥PYRA_MIN_SCORE, añadir una segunda entrada de PYRA_SIZE_PCT
PYRA_ENABLED      = True
PYRA_MIN_RET      = 4.0    # ret mínimo para activar pyramiding (>BEAR_PANIC take_profit=3.0)
PYRA_MIN_HOURS    = 8.0    # horas mínimas en posición antes de añadir
PYRA_MIN_SCORE    = 85     # score mínimo en el momento de añadir
PYRA_SIZE_PCT     = 0.05   # 5% del capital por pyramid (mitad de la entrada inicial)
PYRA_MAX_PER_POS  = 1      # máximo 1 pyramid por posición

# ── POSITION SIZE FIJO SOBRE CAPITAL INICIAL ──────────────────────────────────
# True = siempre usar PAPER2_INITIAL_CAP como base (1.000€ fijos al 10%)
# False = usar capital disponible (se reduce con posiciones abiertas)
# Las entradas tardías en un crash son las mejores — no deben ser irrisoriamente pequeñas
POS_SIZE_ON_INITIAL_CAP = True

# ── SALIDA ANTICIPADA DINÁMICA ────────────────────────────────────────────────
# Si antes de cumplir hold_hours el trade tiene beneficio Y el score ha bajado,
# salir sin esperar — libera capital para nuevas señales
EARLY_EXIT_ENABLED   = True
EARLY_EXIT_MIN_HOURS = 8.0    # no salir antes de 8h
EARLY_EXIT_MIN_RET   = 3.0    # ret mínimo para salida anticipada
EARLY_EXIT_MAX_SCORE = 80     # salir si score cayó por debajo de este umbral

# ── LIBERACIÓN ANTICIPADA DE COOLDOWN ────────────────────────────────────────
# Si el ticker en cooldown de trailing (24h) tiene score >=85 tras 12h,
# liberarlo para no perder señales buenas
COOLDOWN_EARLY_CHECK_HOURS = 12    # revisar tras N horas de cooldown trailing
COOLDOWN_EARLY_MIN_SCORE   = 85    # score mínimo para liberación anticipada

# ── VIX CIRCUIT BREAKER ───────────────────────────────────────────────────────
# Si el VIX suavizado (SMA5) supera este umbral, pausar TODAS las nuevas entradas.
# VIX > 35 históricamente predice caídas adicionales — las señales de sobreventa
# son trampas en pánico real. El sistema sigue gestionando posiciones abiertas.
VIX_CIRCUIT_BREAKER        = True
VIX_CIRCUIT_BREAKER_LEVEL  = 35.0  # VIX SMA5 por encima → no nuevas entradas

# ── VOLUMEN MÍNIMO DE ENTRADA ────────────────────────────────────────────────
# No entrar si el volumen relativo es menor que este umbral.
# vol_rel < 0.6 → señal poco fiable, spread elevado, riesgo de trampa.
# En mercado cerrado el vol_rel es None → el filtro se salta automáticamente.
VOL_MIN_ENTRY              = 0.6   # vol relativo mínimo para abrir posición

# ── ATR MÁXIMO DE ENTRADA ─────────────────────────────────────────────────────
# No entrar en activos con ATR > este umbral.
# ATR alto en crashes = volatilidad extrema = señales falsas y stops muy amplios.
# COIN, TSLA, ARKK pueden tener ATR >8% en pánico.
ATR_MAX_ENTRY              = 5.0   # % máximo de ATR para abrir posición

# ── ESTRATEGIA: RSI DIVERGENCE ────────────────────────────────────────────────
# Si hay divergencia RSI alcista (precio nuevo mínimo pero RSI no),
# bajar el umbral de score a RSI_DIV_MIN_SCORE (vs 85 normal).
# Edge documentado: ~75% WR en divergencias RSI alcistas en activos líquidos.
RSI_DIV_ENABLED   = True
RSI_DIV_MIN_SCORE = 78    # score mínimo cuando hay divergencia RSI bullish

# ── ESTRATEGIA: DIVIDEND YIELD FILTER ────────────────────────────────────────
# Si el div_yield del ticker supera DIV_YIELD_MIN_PCT,
# bajar el umbral de score a DIV_MIN_SCORE.
# Lógica: inversores institucionales compran en caídas para capturar yield alto.
DIV_YIELD_ENABLED  = True
DIV_YIELD_MIN_PCT  = 2.0   # % de dividendo mínimo para activar el filtro
DIV_MIN_SCORE      = 78    # score mínimo cuando div_yield >= DIV_YIELD_MIN_PCT

# ── ESTRATEGIA: BONDS SCORE REDUCIDO ─────────────────────────────────────────
# TLT, SHY, AGG, HYG, TIP raramente llegan a score 85 (poca volatilidad).
# Umbral propio más bajo para capturar sus señales de sobreventa.
# Stop más ajustado porque el ATR de bonos es muy bajo (~0.3-0.8%).
BONDS_ENABLED    = True
BONDS_MIN_SCORE  = 75     # score mínimo para BONDS group
BONDS_TICKERS    = {"TLT", "SHY", "AGG", "HYG", "TIP"}

# ── COOLDOWN DIFERENCIADO ────────────────────────────────────────────────────
# Stop loss = fallo real → 48h cooldown
# Trailing stop / score / tiempo = salida técnica correcta → 24h cooldown
COOLDOWN_STOP_HOURS     = 36   # stop loss activado (rebotes rápidos en 36h)
COOLDOWN_TRAILING_HOURS = 16   # salida por trailing, score o tiempo

# ── TAMAÑO DE POSICIÓN POR VOLATILIDAD ───────────────────────────────────────
# ATR bajo (activos estables) → posición más grande
# ATR alto (activos volátiles) → posición más pequeña
# Riesgo en EUR por trade se iguala entre activos
POS_VOL_LOW_ATR   = 1.5    # ATR% ≤ 1.5 → 13% del capital
POS_VOL_MED_ATR   = 3.0    # ATR% ≤ 3.0 → 10% del capital (estándar)
POS_VOL_HIGH_ATR  = 99.0   # ATR% > 3.0 → 7% del capital
POS_PCT_LOW_VOL   = 0.15
POS_PCT_MED_VOL   = 0.11
POS_PCT_HIGH_VOL  = 0.07

# ── SALIDA ESCALONADA EN 3 TRAMOS ────────────────────────────────────────────
# Tramo 1: al activar trailing (take_profit alcanzado) — asegurar ganancia
# Tramo 2: cuando trailing_drop = -50% del trailing_pct — proteger más
# Tramo 3: al trailing stop final — capturar el máximo posible
# Reduce drawdown medio por trade ~30% vs salida en 2 tramos
PARTIAL_SELL_ENABLED = True
TRANCHE_1_PCT        = 0.33   # vender 33% al activar trailing
TRANCHE_2_PCT        = 0.33   # vender 33% al 50% del trailing stop
# El 34% restante sale al trailing stop final

# ── LÍMITE DE EXPOSICIÓN SECTORIAL ──────────────────────────────────────────
# Máximo N posiciones por sector GICS. Evita exposición concentrada.
SECTOR_LIMIT_ENABLED = True
SECTOR_MAX_POSITIONS = 2
SECTOR_MAP = {
    "NVDA":"SEMI","AMD":"SEMI","AVGO":"SEMI","QCOM":"SEMI","AMAT":"SEMI","LRCX":"SEMI","SOXX":"SEMI",
    "MSFT":"SW","CRM":"SW","ADBE":"SW","NOW":"SW","ORCL":"SW","SNOW":"SW","DDOG":"SW","PANW":"SW",
    "AAPL":"BIGTECH","GOOGL":"BIGTECH","AMZN":"BIGTECH","META":"BIGTECH","NFLX":"BIGTECH","BKNG":"BIGTECH","TSLA":"BIGTECH",
    "BAC":"FIN","JPM":"FIN","GS":"FIN","MS":"FIN","V":"FIN","MA":"FIN","SPGI":"FIN","BLK":"FIN","COIN":"FIN","HOOD":"FIN",
    "UNH":"HEALTH","LLY":"HEALTH","ABBV":"HEALTH","MRNA":"HEALTH","ISRG":"HEALTH","XBI":"HEALTH","IBB":"HEALTH",
    "XOM":"ENERGY","CVX":"ENERGY","EOG":"ENERGY",
    "CAT":"IND","DE":"IND","RTX":"IND",
    "GLD":"METALS","SLV":"METALS","PPLT":"METALS","GDX":"METALS","GDXJ":"METALS","SILJ":"METALS",
    "COPX":"METALS","XME":"METALS","NEM":"METALS","FCX":"METALS","AG":"METALS","GOLD":"METALS",
    "TLT":"BONDS","SHY":"BONDS","AGG":"BONDS","HYG":"BONDS","TIP":"BONDS",
    "PLTR":"DEF_TECH","ARKK":"THEMATIC","COST":"RETAIL",
}

# ── BOLLINGER SQUEEZE ────────────────────────────────────────────────────────
# BB width < percentil 20 → compresión → expansión inminente = breakout
BB_SQUEEZE_ENABLED    = True
BB_SQUEEZE_BONUS      = 5
BB_SQUEEZE_LOOKBACK   = 20
BB_SQUEEZE_PERCENTILE = 20

# ── CONFLUENCIA DE SEÑALES ───────────────────────────────────────────────────
CONFLUENCE_ENABLED    = True
CONFLUENCE_BONUS      = 5      # bonus cuando ≥3 indicadores positivos

# ── RET 5D SCORING ──────────────────────────────────────────────────────────
RET5D_SCORING_ENABLED = True
RET5D_MAX_POINTS      = 5

# ── GAP OVERNIGHT FILTER ────────────────────────────────────────────────────
GAP_FILTER_ENABLED    = True
GAP_FILTER_PCT        = -3.0   # gap down máximo permitido (%)

# ── TRAILING PROGRESIVO POR DURACIÓN ────────────────────────────────────────
PROGRESSIVE_TRAILING_ENABLED = True
PROGRESSIVE_TRAILING = {
    8: 1.5, 24: 1.2, 48: 0.9, 999: 0.7,
}

# ── TRAILING RATCHET — SUELO DE GANANCIA QUE SOLO SUBE ──────────────────────
# Cuando ret_pct alcanza un umbral, se fija un suelo mínimo de ganancia.
# El suelo solo sube, nunca baja. Si el precio cae por debajo del suelo → salir.
# Esto reemplaza el trailing clásico (% desde pico) por un sistema más inteligente.
# Ejemplo: al llegar a +5%, el suelo es +2%. Aunque el precio suba a +12% y baje,
# NUNCA cerrará por debajo de +8% (el suelo más alto alcanzado).
# Formato: {ret_threshold: floor_pct} — ordenado de menor a mayor
RATCHET_ENABLED = True
RATCHET_LEVELS = {
    3.0:  1.0,    # al +3% → suelo +1% (mínimo garantizado)
    5.0:  2.5,    # al +5% → suelo +2.5%
    8.0:  5.0,    # al +8% → suelo +5%
    12.0: 8.0,    # al +12% → suelo +8%
    18.0: 13.0,   # al +18% → suelo +13%
    25.0: 19.0,   # al +25% → suelo +19% (mega rally)
}

# ── DCA INTRADAY — ENTRADA ESCALONADA ────────────────────────────────────────
DCA_ENTRY_ENABLED     = True
DCA_TRANCHE_1_PCT     = 0.50   # 50% inmediato
DCA_TRANCHE_2_PCT     = 0.30   # 30% tras 3h
DCA_TRANCHE_2_HOURS   = 3.0
DCA_TRANCHE_3_PCT     = 0.20   # 20% tras 6h
DCA_TRANCHE_3_HOURS   = 6.0
DCA_MIN_SCORE_HOLD    = 80     # score mínimo para tramos 2 y 3

# ── TICKERS NO OPERABLES EN ALPACA/XTB ───────────────────────────────────────
# Solo para monitoreo de precios — el paper trading nunca debe abrirlos
NON_TRADEABLE = {
    # Índices europeos puros (no son ETFs comprables)
    "^FCHI", "^GDAXI", "^AEX", "^IBEX", "^SSMI",
    # Futuros — Alpaca no opera futuros; XTB con símbolo distinto
    "BZ=F", "CL=F", "NG=F", "ZW=F",
    # Divisas — yfinance format, no operable en ninguna plataforma
    "EURUSD=X", "JPY=X", "GBPUSD=X", "CHF=X", "CNY=X",
    # ETF en LSE (GBP) — no disponible en Alpaca
    "ISF.L",
}

# ── MOMENTUM MODE — parámetros diferenciados ──────────────────────────────────
# Hold más largo para que la tendencia se desarrolle,
# pero trailing más agresivo para asegurar ganancias antes
MOM_HOLD_HOURS   = 60.0   # hold mínimo: 60h (~2.5 días)
MOM_MAX_HOURS    = 72.0   # hold máximo: 72h (3 días)
MOM_TAKE_PROFIT  = 2.0    # activa trailing desde +2% (vs +5% en dip)
MOM_TRAILING_MIN = 2.0    # trailing mínimo desde el que se activa

# ── ATR TRAILING STOP (reemplaza el trailing fijo del 2%) ────────────────────
ATR_TRAILING_MULT  = 1.5     # multiplicador del ATR
ATR_TRAILING_FLOOR = 1.5     # % mínimo trailing (activos estables: SHY, AGG)
ATR_TRAILING_CAP   = 5.0     # % máximo trailing (activos volátiles: ARKK, COIN)
ATR_DEFAULT_PCT    = 2.0     # fallback si no hay ATR

# ── TICKERS ───────────────────────────────────────────────────────────────────
GROUPS = {
    "MAJOR INDICES": [
        ("MSCI World","URTH"),("MSCI Emerging Mkts","EEM"),("Russell 2000","IWM"),
        ("S&P 500 Eq. Weight","RSP"),("S&P 500 ETF","SPY"),("NASDAQ 100 ETF","QQQ"),
    ],
    "MAG 7": [
        ("NVIDIA","NVDA"),("Apple","AAPL"),("Alphabet","GOOGL"),("Microsoft","MSFT"),
        ("Amazon","AMZN"),("Meta","META"),("Tesla","TSLA"),
    ],
    "US STOCKS": [
        # Tecnología
        ("AMD","AMD"),("Broadcom","AVGO"),("Qualcomm","QCOM"),("Applied Materials","AMAT"),
        ("Salesforce","CRM"),("Adobe","ADBE"),("ServiceNow","NOW"),
        # Financieros
        ("Bank of America","BAC"),("JPMorgan","JPM"),("Goldman Sachs","GS"),
        ("Morgan Stanley","MS"),("Visa","V"),("Mastercard","MA"),
        # Salud / Biotech
        ("UnitedHealth","UNH"),("Eli Lilly","LLY"),("AbbVie","ABBV"),
        ("Moderna","MRNA"),("Intuitive Surgical","ISRG"),
        # Consumo / Retail
        ("Netflix","NFLX"),("Booking Holdings","BKNG"),("Costco","COST"),
        # Energía
        ("ExxonMobil","XOM"),("Chevron","CVX"),("EOG Resources","EOG"),
        # Industrial / Defensa
        ("Caterpillar","CAT"),("Deere","DE"),("RTX Corp","RTX"),
        # Cripto / Fintech / Otros
        ("Coinbase","COIN"),("Palantir","PLTR"),("Robinhood","HOOD"),
        # High conviction adicionales
        ("Oracle","ORCL"),("Snowflake","SNOW"),("Datadog","DDOG"),
        ("Lam Research","LRCX"),("Palo Alto Networks","PANW"),
        ("S&P Global","SPGI"),("BlackRock","BLK"),
    ],
    "US SECTORS": [
        ("Technology","XLK"),("Healthcare","XLV"),("Financials","XLF"),
        ("Consumer Discret.","XLY"),("Communication Svcs","XLC"),("Industrials","XLI"),
        ("Consumer Staples","XLP"),("Energy","XLE"),("Utilities","XLU"),
        ("Real Estate","XLRE"),("Materials","XLB"),
    ],
    "BONDS": [
        ("Long Duration US Bonds","TLT"),("Short Duration US Bonds","SHY"),
        ("US Agg Bond","AGG"),("High Yield","HYG"),("TIPS (Inflation)","TIP"),
    ],
    "COMMODITIES": [
        ("Oil (Brent)","BZ=F"),("Oil (WTI)","CL=F"),("Natural Gas","NG=F"),("Wheat","ZW=F"),
    ],
    "METALS & MINING": [
        ("Gold ETF","GLD"),("Silver ETF","SLV"),("Platinum ETF","PPLT"),
        ("Gold Miners","GDX"),("Jr. Gold Miners","GDXJ"),("Jr. Silver Miners","SILJ"),
        ("Copper Miners ETF","COPX"),("Metals & Mining ETF","XME"),
        ("Newmont","NEM"),("Freeport-McMoRan","FCX"),("First Majestic Silver","AG"),("Barrick Gold","GOLD"),
    ],
    "THEMATIC": [
        ("ARK Innovation","ARKK"),("Biotech ETF","IBB"),("Semiconductors ETF","SOXX"),("Reg. Banks ETF","KRE"),
        ("Real Estate ETF","VNQ"),("Small-Cap Biotech","XBI"),("Retail ETF","XRT"),("Airlines ETF","JETS"),
        ("Homebuilders","XHB"),("Clean Energy","ICLN"),("Cybersecurity","CIBR"),
    ],
    "CURRENCIES": [
        ("EUR/USD","EURUSD=X"),("USD/JPY","JPY=X"),("GBP/USD","GBPUSD=X"),
        ("USD/CHF","CHF=X"),("USD/CNY","CNY=X"),
    ],
    "EUROPE": [
        ("UK (FTSE 100)","ISF.L"),("France (CAC 40)","^FCHI"),("Germany (DAX)","^GDAXI"),
        ("Netherlands (AEX)","^AEX"),("Spain (IBEX 35)","^IBEX"),
        ("Italy (FTSE MIB)","EWI"),("Switzerland (SMI)","^SSMI"),
    ],
    "EU SECTORS": [
        ("EU Banks","EUFN"),("EU Healthcare","IXJ"),("EU Energy","IXC"),
        ("EU Technology","IYW"),("EU Telecoms","IXP"),("EU Utilities","JXI"),("EU Materials","PDBC"),
    ],
    "INTERNACIONAL": [
        ("South Korea","EWY"),("China","MCHI"),("Taiwan","EWT"),("Vietnam","VNM"),
        ("Brazil","EWZ"),("Mexico","EWW"),("Argentina","ARGT"),("Chile","ECH"),("Peru","EPU"),
    ],
}

# ── TICKERS USD-DENOMINADOS (fuente única de verdad) ─────────────────────────
# Añadir/quitar tickers SOLO aquí. USD_TICKERS y ALPACA_TRADEABLE se derivan.
# Nota: futuros y divisas excluidos deliberadamente (están en NON_TRADEABLE).
_USD_BASE = {
    # Índices ETF
    "URTH","EEM","IWM","RSP","SPY","QQQ",
    # MAG 7
    "NVDA","AAPL","GOOGL","MSFT","AMZN","META","TSLA",
    # US STOCKS — Tecnología
    "AMD","AVGO","QCOM","AMAT","CRM","ADBE","NOW",
    # US STOCKS — Financieros
    "BAC","JPM","GS","MS","V","MA",
    # US STOCKS — Salud
    "UNH","LLY","ABBV","MRNA","ISRG",
    # US STOCKS — Consumo / Retail
    "NFLX","BKNG","COST",
    # US STOCKS — Energía
    "XOM","CVX","EOG",
    # US STOCKS — Industrial / Defensa
    "CAT","DE","RTX",
    # US STOCKS — Cripto / Fintech
    "COIN","PLTR","HOOD",
    # Sectores US
    "XLK","XLV","XLF","XLY","XLC","XLI","XLP","XLE","XLU","XLRE","XLB",
    # Bonos
    "TLT","SHY","AGG","HYG","TIP",
    # Metales y minería
    "GLD","SLV","PPLT","GDX","GDXJ","SILJ","COPX","XME","NEM","FCX","AG","GOLD",
    # Temáticos
    "ARKK","IBB","SOXX","KRE",
    "VNQ","XBI","XRT","JETS","XHB","ICLN","CIBR",
    # US STOCKS adicionales
    "ORCL","SNOW","DDOG","LRCX","PANW","SPGI","BLK",
    # Internacional USD-listed
    "EWY","MCHI","EWT","VNM","EWZ","EWW","ARGT","ECH","EPU",
    "EUFN","IXJ","IXC","IYW","IXP","JXI","PDBC","EWI",
}

# Todos los tickers USD se consideran operables en Alpaca (salvo NON_TRADEABLE)
USD_TICKERS      = _USD_BASE
ALPACA_TRADEABLE = _USD_BASE

EU_TICKERS = {"ISF.L", "^FCHI", "^GDAXI", "^AEX", "^IBEX", "^SSMI"}
FUTURES_TICKERS = {"BZ=F","CL=F","NG=F","ZW=F","EURUSD=X","JPY=X","GBPUSD=X","CHF=X","CNY=X"}

# ── CACHE TTLs ────────────────────────────────────────────────────────────────
INTRADAY_TTL     = 240
FUND_TTL         = 86400
FUND_MAX         = 100
CHART_TTL        = 900
CHART_MAX        = 50
REFRESH_INTERVAL = 180  # 3 min — más reactividad en mercado abierto
