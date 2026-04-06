# Market Tracker — Versión Simplificada

Versión limpia con solo dos páginas:
- **`/`** — Tabla de mercado con todos los activos, señales y métricas
- **`/paper4`** — Paper trading Combo 24h (Score ≥ 80 + Signal + Bullish → salida 24h)

## Deploy en Railway (recomendado)

### Opción A — GitHub + Railway (más fácil)
1. Crea un repositorio en GitHub y sube estos archivos
2. Ve a [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Railway detecta el `Procfile` y despliega automáticamente
4. (Opcional) Configura variables de entorno para alertas email:
   - `EMAIL_FROM` → tu Gmail
   - `EMAIL_PASSWORD` → contraseña de aplicación Gmail (16 chars)
   - `EMAIL_TO` → email destino

### Opción B — Railway CLI
```bash
npm install -g @railway/cli
railway login
railway init
railway up
```

## Desarrollo local

```bash
pip install -r requirements.txt
python app.py
# Abre http://localhost:5000
```

## Estructura

```
market-tracker/
├── app.py              # Backend Flask (todo en uno)
├── templates/
│   ├── index.html      # Pantalla principal (mercado)
│   └── paper4.html     # Paper trading Combo 24h
├── requirements.txt
├── Procfile
└── railway.json
```

## Estrategia Combo 24h (Paper4)

| Condición entrada | Valor |
|---|---|
| Score mínimo | ≥ 80/100 |
| Signal | buy o strong_buy |
| Tendencia | bullish (SMA50 > SMA200) |

| Condición salida | Valor |
|---|---|
| Take profit | +10% |
| Stop loss | −7% |
| Tiempo máximo | 24h (si positivo) |
| Cooldown tras stop | 48h |

Capital inicial: €10,000 · 20% por posición · Sin comisiones

## Notas Railway

- **1 worker, 4 threads**: suficiente para uso personal (Railway free tier: 512MB RAM)
- El primer refresh tarda ~60s (76 tickers en paralelo)
- Los datos de paper trading se guardan en `paper4_trades.json` (persiste entre reinicios si usas Railway volumes)
- Si el proceso se reinicia, el JSON se pierde en Railway free tier — usa Railway Volumes para persistencia
