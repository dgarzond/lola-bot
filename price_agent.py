"""
Price Tracker Agent — WhatsApp + Railway Cloud
================================================
En Railway corre un solo proceso: Flask webhook + APScheduler para el cron.
No necesitas crontab ni dos terminales.

Deploy:
  1. Sube este repo a GitHub
  2. Conecta en railway.app
  3. Añade las variables de entorno en Railway Dashboard
  4. Railway detecta el Procfile y hace deploy automático
"""

import os, json, datetime, sys, threading
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import anthropic
from twilio.rest import Client as TwilioClient
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler

WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"

# ── Storage ───────────────────────────────────────────────────────────────────

def load_watchlist() -> dict:
    if WATCHLIST_FILE.exists():
        return json.loads(WATCHLIST_FILE.read_text())
    return {}

def save_watchlist(data: dict):
    WATCHLIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

# ── Claude: parsear intención ─────────────────────────────────────────────────

def parse_user_intent(message: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = f"""El usuario mandó este mensaje a un agente de price tracking por WhatsApp:
"{message}"

Responde SOLO con JSON (sin markdown):
{{
  "accion": "agregar" | "eliminar" | "listar" | "comprado" | "desconocido",
  "producto": "nombre del producto con todos los detalles relevantes",
  "precio_objetivo": null o número en euros,
  "numero_item": null o número (si dice 'comprado 2' o 'eliminar 3'),
  "query_busqueda": "query optimizada para buscar precio en Google Shopping"
}}"""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )
    text = resp.content[0].text.strip().strip("```json").strip("```").strip()
    try:
        return json.loads(text)
    except Exception:
        return {"accion": "desconocido"}

# ── Buscar precios ────────────────────────────────────────────────────────────

def search_prices(producto: str, query: str) -> list:
    try:
        from tavily import TavilyClient
    except ImportError:
        return []

    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    queries = [
        f"{query} precio Amazon España",
        f"{query} precio MediaMarkt PCComponentes España",
        f"{query} precio eBay España oferta",
        f"{query} precio El Corte Inglés Fnac",
    ]

    seen, results = set(), []
    for q in queries:
        try:
            for r in client.search(q, max_results=4).get("results", []):
                if r.get("url") not in seen:
                    seen.add(r["url"])
                    results.append(r)
        except Exception:
            pass
    return results

def extract_prices_with_claude(producto: str, raw_results: list) -> list:
    if not raw_results:
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    results_text = "\n\n".join([
        f"URL: {r.get('url')}\nTítulo: {r.get('title')}\nContenido: {r.get('content','')[:500]}"
        for r in raw_results
    ])

    prompt = f"""Analiza resultados de búsqueda para: "{producto}"

{results_text}

Extrae listings reales con precio. Responde SOLO JSON array (sin markdown):
[{{"tienda":"Amazon","precio":89.99,"moneda":"EUR","url":"https://...","descripcion":"nombre producto","disponible":true}}]

Solo precios numéricos claros. Ordena menor a mayor. Si no hay, devuelve []."""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    text = resp.content[0].text.strip().strip("```json").strip("```").strip()
    try:
        return json.loads(text)
    except Exception:
        return []

# ── WhatsApp ──────────────────────────────────────────────────────────────────

def send_whatsapp(body: str):
    client = TwilioClient(
        os.environ["TWILIO_ACCOUNT_SID"],
        os.environ["TWILIO_AUTH_TOKEN"]
    )
    client.messages.create(
        from_=os.environ["TWILIO_WHATSAPP_FROM"],
        to=os.environ["MY_WHATSAPP"],
        body=body
    )

def format_watchlist(watchlist: dict) -> str:
    active = {k: v for k, v in watchlist.items() if v.get("activo", True)}
    if not active:
        return "📋 No tienes productos en seguimiento.\n\nDime qué quieres comprar y lo agrego 👇"

    lines = ["📋 *Productos en seguimiento:*\n"]
    for i, (_, item) in enumerate(active.items(), 1):
        precio = item.get("mejor_precio_actual")
        precio_str = f"€{precio:.2f} en {item.get('mejor_tienda','?')}" if precio else "buscando..."
        objetivo = f" · objetivo €{item['precio_objetivo']}" if item.get("precio_objetivo") else ""
        lines.append(f"{i}. *{item['producto']}*\n   💰 {precio_str}{objetivo}")

    lines.append("\n_Responde *comprado N* para marcar como comprado_")
    return "\n".join(lines)

# ── Check precios (corre 4x/día) ──────────────────────────────────────────────

def check_all_prices():
    """Job del scheduler — revisa todos los productos y manda alertas."""
    print(f"⏰ Revisando precios — {datetime.datetime.now().strftime('%H:%M')}")
    watchlist = load_watchlist()
    active = {k: v for k, v in watchlist.items() if v.get("activo", True)}

    if not active:
        print("   Watchlist vacía.")
        return

    now = datetime.datetime.now().isoformat()

    for item_id, item in active.items():
        print(f"  → {item['producto']}")
        raw = search_prices(item["producto"], item.get("query_busqueda", item["producto"]))
        prices = extract_prices_with_claude(item["producto"], raw)

        if not prices:
            print(f"    Sin precios encontrados")
            continue

        best = prices[0]
        nuevo_precio = best["precio"]
        prev_precio = item.get("mejor_precio_actual")

        if "historial" not in item:
            item["historial"] = []
        item["historial"].append({
            "fecha": now,
            "precio": nuevo_precio,
            "tienda": best["tienda"],
            "url": best["url"]
        })
        item["mejor_precio_actual"] = nuevo_precio
        item["mejor_tienda"] = best["tienda"]
        item["mejor_url"] = best["url"]
        item["ultima_revision"] = now

        print(f"    💰 €{nuevo_precio:.2f} en {best['tienda']}")

        # Alerta si bajó
        if prev_precio is not None and nuevo_precio < prev_precio:
            bajada = prev_precio - nuevo_precio
            pct = (bajada / prev_precio) * 100
            item_num = list(active.keys()).index(item_id) + 1
            msg = (
                f"🔔 *Bajada de precio!*\n\n"
                f"🛍️ *{item['producto']}*\n"
                f"💰 *€{nuevo_precio:.2f}* (antes €{prev_precio:.2f})\n"
                f"📉 Bajó €{bajada:.2f} ({pct:.1f}%)\n"
                f"🏪 {best['tienda']}\n"
                f"🔗 {best['url']}\n\n"
                f"Responde *comprado {item_num}* para dejar de trackear."
            )
            send_whatsapp(msg)
            print(f"    📱 Alerta enviada!")

        # Alerta si alcanzó precio objetivo
        if item.get("precio_objetivo") and nuevo_precio <= item["precio_objetivo"]:
            msg = (
                f"🎯 *Precio objetivo alcanzado!*\n\n"
                f"🛍️ *{item['producto']}*\n"
                f"💰 *€{nuevo_precio:.2f}* (tu objetivo: €{item['precio_objetivo']})\n"
                f"🏪 {best['tienda']}\n"
                f"🔗 {best['url']}"
            )
            send_whatsapp(msg)

        watchlist[item_id] = item

    save_watchlist(watchlist)
    print(f"   ✅ Revisión completada.")

def check_single_async(item_id: str):
    """Busca precio inicial de un producto recién agregado."""
    def _run():
        import time; time.sleep(3)
        watchlist = load_watchlist()
        if item_id not in watchlist:
            return
        item = watchlist[item_id]
        raw = search_prices(item["producto"], item.get("query_busqueda", item["producto"]))
        prices = extract_prices_with_claude(item["producto"], raw)
        if prices:
            best = prices[0]
            item["mejor_precio_actual"] = best["precio"]
            item["mejor_tienda"] = best["tienda"]
            item["mejor_url"] = best["url"]
            item["historial"] = [{"fecha": datetime.datetime.now().isoformat(), "precio": best["precio"], "tienda": best["tienda"], "url": best["url"]}]
            watchlist[item_id] = item
            save_watchlist(watchlist)
            send_whatsapp(
                f"✅ *Precio inicial encontrado!*\n\n"
                f"🛍️ *{item['producto']}*\n"
                f"💰 Mejor precio ahora: *€{best['precio']:.2f}*\n"
                f"🏪 {best['tienda']}\n"
                f"🔗 {best['url']}\n\n"
                f"Te aviso cuando baje 🔔"
            )
    threading.Thread(target=_run, daemon=True).start()

# ── Flask webhook ─────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    message = request.form.get("Body", "").strip()
    print(f"📩 {message}")

    watchlist = load_watchlist()
    intent = parse_user_intent(message)
    accion = intent.get("accion", "desconocido")

    if accion == "agregar":
        producto = intent.get("producto", message)
        query = intent.get("query_busqueda", producto)
        precio_objetivo = intent.get("precio_objetivo")
        item_id = f"{producto[:30].lower().replace(' ', '-')}-{datetime.date.today().isoformat()}"
        watchlist[item_id] = {
            "producto": producto,
            "query_busqueda": query,
            "precio_objetivo": precio_objetivo,
            "mejor_precio_actual": None,
            "activo": True,
            "agregado": datetime.datetime.now().isoformat(),
            "historial": []
        }
        save_watchlist(watchlist)
        obj_str = f"\n🎯 Precio objetivo: €{precio_objetivo}" if precio_objetivo else ""
        reply = f"👀 Agregado al tracker!\n\n🛍️ *{producto}*{obj_str}\n\nBuscando el mejor precio ahora, te aviso en un momento..."
        check_single_async(item_id)

    elif accion == "listar":
        reply = format_watchlist(watchlist)

    elif accion in ("comprado", "eliminar"):
        num = intent.get("numero_item")
        active_keys = [k for k, v in watchlist.items() if v.get("activo", True)]
        if num and 1 <= num <= len(active_keys):
            iid = active_keys[num - 1]
            nombre = watchlist[iid]["producto"]
            watchlist[iid]["activo"] = False
            watchlist[iid]["fecha_compra"] = datetime.date.today().isoformat()
            save_watchlist(watchlist)
            reply = f"🎉 Listo! Dejé de trackear:\n*{nombre}*\n\n¿Qué más quieres buscar?"
        else:
            reply = "⚠️ Dime el número. Escribe *listar* para ver tus productos."

    else:
        reply = (
            "No entendí 🤔 Puedes decirme:\n\n"
            "• *quiero comprar [producto]* — agregar tracking\n"
            "• *listar* — ver qué estoy trackeando\n"
            "• *comprado N* — marcar como comprado\n"
            "• *eliminar N* — dejar de trackear"
        )

    send_whatsapp(reply)
    return "", 204

@app.route("/health", methods=["GET"])
def health():
    watchlist = load_watchlist()
    active = sum(1 for v in watchlist.values() if v.get("activo", True))
    return {"status": "ok", "productos_activos": active, "hora": datetime.datetime.now().isoformat()}, 200

# ── Main: arranca Flask + Scheduler juntos ────────────────────────────────────

if __name__ == "__main__":
    # Scheduler: revisar precios 4 veces al día (8, 14, 20, 2)
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_all_prices, "cron", hour="8,14,20,2", minute=0)
    scheduler.start()
    print("⏰ Scheduler activo — revisará precios a las 8, 14, 20 y 2h")

    port = int(os.environ.get("PORT", 5001))
    print(f"🌐 Webhook en puerto {port}")
    print("✅ Price Tracker Agent corriendo en Railway!\n")

    app.run(host="0.0.0.0", port=port, debug=False)
