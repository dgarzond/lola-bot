# Price Tracker Agent 🛍️

Agente que trackea precios por WhatsApp. Le dices qué quieres comprar, él busca en Amazon, MediaMarkt, eBay, etc., y te avisa cuando baja el precio.

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
| `TWILIO_ACCOUNT_SID` | de console.twilio.com |
| `TWILIO_AUTH_TOKEN` | de console.twilio.com |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+14155238886` |
| `MY_WHATSAPP` | `whatsapp:+34600000000` |

### 4. Conecta Twilio con tu Railway URL

1. En Railway → tu proyecto → **Settings** → copia la URL pública (ej: `https://price-tracker.up.railway.app`)
2. Ve a [Twilio Console](https://console.twilio.com) → Messaging → Try it out → Send a WhatsApp message
3. En **Sandbox Settings** → campo "When a message comes in" → pega:
   ```
   https://price-tracker.up.railway.app/webhook
   ```

### 5. Activa el sandbox de Twilio en tu WhatsApp

Twilio te da un código tipo `join <palabra>`. Mándalo desde tu WhatsApp al número del sandbox (+1 415 523 8886).

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
Tu WhatsApp
    ↓ mensaje
Twilio Sandbox
    ↓ POST
Railway (Flask webhook) ──→ Claude (parsea intención)
    ↓ guarda en watchlist.json
APScheduler (cada 6h)
    ↓ Tavily (busca precios)
    ↓ Claude (extrae precios)
    ↓ si bajó precio
Tu WhatsApp ← Twilio ← Railway
```

## Archivos

```
price_agent.py   # agente principal (webhook + scheduler)
Procfile         # Railway: web: python price_agent.py
requirements.txt # dependencias
.env.example     # variables de entorno de ejemplo
watchlist.json   # se crea automáticamente con tus productos
```
