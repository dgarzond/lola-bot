# Price Tracker Agent 🛍️ (Telegram)

Agente que trackea precios por Telegram (multiusuario). Le dices qué quieres comprar, él busca en internet y te avisa cuando baja el precio.

## Deploy en Railway (10 minutos)

### 1. Sube el código a GitHub

```bash
git init
git add .
git commit -m "price tracker agent"
# Crea un repo en github.com y sigue las instrucciones
git remote add origin https://github.com/TU_USUARIO/price-tracker.git
git push -u origin main
```

### 2. Deploy en Railway

1. Ve a [railway.app](https://railway.app) → **New Project**
2. Elige **Deploy from GitHub repo**
3. Selecciona tu repo `price-tracker`
4. Railway detecta el `Procfile` automáticamente ✅

### 3. Añade las variables de entorno en Railway

En tu proyecto Railway → **Variables** → añade:

| Variable | Valor |
|---|---|
| `ANTHROPIC_API_KEY` | tu key de Anthropic |
| `TAVILY_API_KEY` | tu key de Tavily (gratis en app.tavily.com) |
| `TELEGRAM_BOT_TOKEN` | token del bot (BotFather) |

### 4. Crea el bot en Telegram (BotFather)

1. En Telegram abre `@BotFather`
2. Ejecuta `/newbot`
3. Copia el token y guárdalo como `TELEGRAM_BOT_TOKEN` en Railway

### 5. Conecta Telegram con tu Railway URL (setWebhook)

1. En Railway → tu proyecto → copia la URL pública (ej: `https://lola-bot.up.railway.app`)
2. Abre en tu navegador:
   ```
   https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://TU_URL.up.railway.app/telegram-webhook
   ```
3. Si responde `{"ok":true,...}` quedó listo ✅

---

## Uso

| Mensaje | Qué hace |
|---|---|
| `quiero comprar Nike Air Max 90 talla 42` | Agrega tracking |
| `quiero un iPhone 15 por menos de 700 euros` | Agrega con precio objetivo |
| `listar` | Ver productos en seguimiento |
| `comprado 1` | Marca como comprado y para el tracking |
| `eliminar 2` | Deja de trackear sin marcar comprado |

---

## Arquitectura

```
Telegram
    ↓ mensaje
Telegram webhook
    ↓ POST
Railway (Flask webhook) ──→ Claude (parsea intención)
    ↓ guarda en watchlist.json
APScheduler (cada 6h)
    ↓ Tavily (busca precios)
    ↓ Claude (extrae precios)
    ↓ si bajó precio
Telegram ← Railway
```

## Archivos

```
price_agent.py   # agente principal (webhook + scheduler)
Procfile         # Railway: web: python price_agent.py
requirements.txt # dependencias
.env.example     # variables de entorno de ejemplo
watchlist.json   # se crea automáticamente con tus productos
```
