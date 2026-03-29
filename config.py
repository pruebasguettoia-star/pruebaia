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
PAPER_DATA_DIR      = os.environ.get("PAPER_DATA_DIR", os.path.dirname(__file__))
PAPER2_FILE         = os.path.join(PAPER_DATA_DIR, "paper2_trades.json")
PAPER2_INITIAL_CAP  = 10_000.0
PAPER2_POSITION_PCT = 0.10
PAPER2_MIN_SCORE    = 85
PAPER2_HOLD_HOURS   = 24
PAPER2_STOP_LOSS    = -7.0
PAPER2_TAKE_PROFIT  = 5.0
PAPER2_TRAILING_MIN = 3.0
PAPER2_MAX_HOURS    = 48.0

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
        ("AMD","AMD"),("Bank of America","BAC"),("Coinbase","COIN"),
        ("JPMorgan","JPM"),("Netflix","NFLX"),("Palantir","PLTR"),
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
    "AMD","BAC","COIN","JPM","NFLX","PLTR",
    "XLK","XLV","XLF","XLY","XLC","XLI","XLP","XLE","XLU","XLRE","XLB",
    "TLT","SHY","AGG","HYG","TIP","BZ=F","CL=F","NG=F","ZW=F",
    "GLD","SLV","PPLT","GDX","GDXJ","SILJ","COPX","XME","NEM","FCX","AG","GOLD",
    "ARKK","IBB","SOXX","KRE",
    "EWY","MCHI","EWT","VNM","EWZ","EWW","ARGT","ECH","EPU",
    "EUFN","IXJ","IXC","IYW","IXP","JXI","PDBC","EWI",
}

ALPACA_TRADEABLE = {
    "NVDA","AAPL","GOOGL","MSFT","AMZN","META","TSLA",
    "AMD","BAC","COIN","JPM","NFLX","PLTR",
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
