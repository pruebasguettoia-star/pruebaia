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
EMAIL_FROM     = os.environ.get("EMAIL_FROM", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")
EMAIL_ENABLED  = bool(EMAIL_FROM and EMAIL_PASSWORD and EMAIL_TO)

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
    "BULL_QUIET":    8.0,   # activa trailing en +8% — deja correr tendencias
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
HOLD_EXT_SCORE     = 88     # score mínimo para extensión
HOLD_EXT_RET_PCT   = 3.0   # ret mínimo en % para extensión
HOLD_EXT_MAX_HOURS = 72.0  # hold máximo extendido

# ── PYRAMIDING ───────────────────────────────────────────────────────────────
# Si una posición lleva ≥PYRA_MIN_RET% y ≥PYRA_MIN_HOURS,
# y el score sigue ≥PYRA_MIN_SCORE, añadir una segunda entrada de PYRA_SIZE_PCT
PYRA_ENABLED      = True
PYRA_MIN_RET      = 3.0    # ret mínimo para activar pyramiding
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

# ── COOLDOWN DIFERENCIADO ────────────────────────────────────────────────────
# Stop loss = fallo real → 48h cooldown
# Trailing stop / score / tiempo = salida técnica correcta → 24h cooldown
COOLDOWN_STOP_HOURS     = 48   # stop loss activado
COOLDOWN_TRAILING_HOURS = 24   # salida por trailing, score o tiempo

# ── TAMAÑO DE POSICIÓN POR VOLATILIDAD ───────────────────────────────────────
# ATR bajo (activos estables) → posición más grande
# ATR alto (activos volátiles) → posición más pequeña
# Riesgo en EUR por trade se iguala entre activos
POS_VOL_LOW_ATR   = 1.5    # ATR% ≤ 1.5 → 13% del capital
POS_VOL_MED_ATR   = 3.0    # ATR% ≤ 3.0 → 10% del capital (estándar)
POS_VOL_HIGH_ATR  = 99.0   # ATR% > 3.0 → 7% del capital
POS_PCT_LOW_VOL   = 0.13
POS_PCT_MED_VOL   = 0.10
POS_PCT_HIGH_VOL  = 0.07

# ── SALIDA PARCIAL EN TRAILING ───────────────────────────────────────────────
# Al activarse el trailing, vender PARTIAL_SELL_PCT de la posición
# y dejar el resto correr con el trailing habitual
PARTIAL_SELL_ENABLED = True
PARTIAL_SELL_PCT     = 0.50   # vender 50% al activar trailing

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

USD_TICKERS = {
    "URTH","EEM","IWM","RSP","SPY","QQQ",
    "NVDA","AAPL","GOOGL","MSFT","AMZN","META","TSLA",
    # US STOCKS expanded
    "AMD","AVGO","QCOM","AMAT","CRM","ADBE","NOW",
    "BAC","JPM","GS","MS","V","MA",
    "UNH","LLY","ABBV","MRNA","ISRG",
    "NFLX","BKNG","COST",
    "XOM","CVX","EOG",
    "CAT","DE","RTX",
    "COIN","PLTR","HOOD",
    # Sectores US
    "XLK","XLV","XLF","XLY","XLC","XLI","XLP","XLE","XLU","XLRE","XLB",
    # Bonos
    "TLT","SHY","AGG","HYG","TIP",
    # Metales y minería
    "GLD","SLV","PPLT","GDX","GDXJ","SILJ","COPX","XME","NEM","FCX","AG","GOLD",
    # Temáticos
    "ARKK","IBB","SOXX","KRE",
    # Internacional USD-listed
    "EWY","MCHI","EWT","VNM","EWZ","EWW","ARGT","ECH","EPU",
    "EUFN","IXJ","IXC","IYW","IXP","JXI","PDBC","EWI",
    # Nota: futuros y divisas excluidos deliberadamente
}

ALPACA_TRADEABLE = {
    "NVDA","AAPL","GOOGL","MSFT","AMZN","META","TSLA",
    # US STOCKS expanded
    "AMD","AVGO","QCOM","AMAT","CRM","ADBE","NOW",
    "BAC","JPM","GS","MS","V","MA",
    "UNH","LLY","ABBV","MRNA","ISRG",
    "NFLX","BKNG","COST",
    "XOM","CVX","EOG",
    "CAT","DE","RTX",
    "COIN","PLTR","HOOD",
    # ETFs
    "XLK","XLV","XLF","XLY","XLC","XLI","XLP","XLE","XLU","XLRE","XLB",
    "TLT","SHY","AGG","HYG","TIP",
    "URTH","EEM","IWM","RSP","SPY","QQQ",
    "GLD","SLV","PPLT","GDX","GDXJ","SILJ","COPX","XME","NEM","FCX","AG","GOLD",
    "ARKK","IBB","SOXX","KRE",
    "EWY","MCHI","EWT","VNM","EWZ","EWW","ARGT","ECH","EPU",
    "EUFN","IXJ","IXC","IYW","IXP","JXI","PDBC","EWI",
}

EU_TICKERS = {"ISF.L", "^FCHI", "^GDAXI", "^AEX", "^IBEX", "^SSMI"}
FUTURES_TICKERS = {"BZ=F","CL=F","NG=F","ZW=F","EURUSD=X","JPY=X","GBPUSD=X","CHF=X","CNY=X"}

# ── CACHE TTLs ────────────────────────────────────────────────────────────────
INTRADAY_TTL     = 240
FUND_TTL         = 86400
FUND_MAX         = 100
CHART_TTL        = 900
CHART_MAX        = 10
REFRESH_INTERVAL = 300
