"""
Price Tracker Agent — Telegram + Railway Cloud
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
from urllib.parse import urlsplit, urlunsplit
import requests
import redis
from redis.exceptions import AuthenticationError, RedisError
import re

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import anthropic
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler

WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"

# ── Config / env ──────────────────────────────────────────────────────────────

def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value

# ── Redis (optional) ──────────────────────────────────────────────────────────

_redis_client = None

def get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        _redis_client = None
        return None

    try:
        client = redis.Redis.from_url(redis_url, decode_responses=True)
        # Valida credenciales/conectividad ahora (evita 500 en runtime).
        client.ping()
        _redis_client = client
        return _redis_client
    except AuthenticationError as e:
        print(f"❌ Redis auth error (check REDIS_URL): {e}")
        _redis_client = None
        return None
    except RedisError as e:
        print(f"❌ Redis error (check REDIS_URL/network): {e}")
        _redis_client = None
        return None

def _redis_watchlist_key(chat_key: str) -> str:
    return f"watchlist:{chat_key}"

REDIS_CHATS_KEY = "watchlist:chats"

def _redis_pending_key(chat_key: str) -> str:
    return f"pending_add:{chat_key}"

def _redis_pending_batch_key(chat_key: str) -> str:
    return f"pending_batch:{chat_key}"

# ── Telegram ─────────────────────────────────────────────────────────────────

def telegram_send(chat_id: int | str, text: str):
    token = require_env("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    max_chars = int(os.getenv("TELEGRAM_MAX_CHARS", "3500"))
    t = (text or "").strip()
    chunks = []
    while t:
        chunk = t[:max_chars]
        cut = chunk.rfind("\n")
        if cut > 500:
            chunk = chunk[:cut]
        chunks.append(chunk.strip())
        t = t[len(chunk):].lstrip("\n")

    for chunk in chunks or [""]:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            timeout=20,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Telegram sendMessage failed: {resp.status_code} {resp.text[:300]}")

# ── Storage (multiusuario) ────────────────────────────────────────────────────

def _migrate_watchlist_if_needed(data: dict) -> dict:
    """
    Formato nuevo (multiusuario):
      {"<chat_id>": {"<item_id>": {...}}, ...}

    Formato viejo (single-user):
      {"<item_id>": {...}, ...}
    """
    if not isinstance(data, dict) or not data:
        return {}

    sample_val = next(iter(data.values()))
    if isinstance(sample_val, dict) and "producto" in sample_val:
        return {"default": data}
    return data

def load_watchlist() -> dict:
    r = get_redis()
    if r is not None:
        data = {}
        try:
            chat_keys = r.smembers(REDIS_CHATS_KEY) or set()
            for chat_key in chat_keys:
                raw = r.get(_redis_watchlist_key(chat_key))
                if raw:
                    data[str(chat_key)] = json.loads(raw)
        except Exception as e:
            print(f"❌ Redis load_watchlist failed, falling back to file: {e}")
            return {}
        return data

    if WATCHLIST_FILE.exists():
        data = json.loads(WATCHLIST_FILE.read_text())
        return _migrate_watchlist_if_needed(data)
    return {}

def save_watchlist(data: dict):
    r = get_redis()
    if r is not None:
        # Guardado “best-effort”: escribe por chat para que el scheduler lo recorra luego.
        try:
            pipe = r.pipeline()
            for chat_key, chat_watchlist in (data or {}).items():
                if not isinstance(chat_watchlist, dict):
                    continue
                pipe.sadd(REDIS_CHATS_KEY, str(chat_key))
                pipe.set(_redis_watchlist_key(str(chat_key)), json.dumps(chat_watchlist, ensure_ascii=False))
            pipe.execute()
            return
        except Exception as e:
            print(f"❌ Redis save_watchlist failed, falling back to file: {e}")

    WATCHLIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def load_chat_watchlist(chat_key: str) -> dict:
    r = get_redis()
    if r is not None:
        try:
            raw = r.get(_redis_watchlist_key(chat_key))
            if not raw:
                return {}
            wl = json.loads(raw)
            return wl if isinstance(wl, dict) else {}
        except Exception as e:
            print(f"❌ Redis load_chat_watchlist failed, falling back to file: {e}")
            return {}

    data = load_watchlist()
    wl = data.get(chat_key)
    if isinstance(wl, dict):
        return wl
    return {}

def save_chat_watchlist(chat_key: str, chat_watchlist: dict):
    r = get_redis()
    if r is not None:
        try:
            r.sadd(REDIS_CHATS_KEY, str(chat_key))
            r.set(_redis_watchlist_key(chat_key), json.dumps(chat_watchlist or {}, ensure_ascii=False))
            return
        except Exception as e:
            print(f"❌ Redis save_chat_watchlist failed, falling back to file: {e}")

    data = load_watchlist()
    data[chat_key] = chat_watchlist
    save_watchlist(data)

def load_pending_add(chat_key: str) -> dict | None:
    r = get_redis()
    if r is not None:
        try:
            raw = r.get(_redis_pending_key(chat_key))
            if not raw:
                return None
            val = json.loads(raw)
            return val if isinstance(val, dict) else None
        except Exception as e:
            print(f"❌ Redis load_pending_add failed: {e}")
            return None

    data = load_watchlist()
    pending = None
    if isinstance(data.get("__pending__"), dict):
        pending = data["__pending__"].get(chat_key)
    return pending if isinstance(pending, dict) else None

def save_pending_add(chat_key: str, payload: dict | None):
    r = get_redis()
    if r is not None:
        try:
            if payload is None:
                r.delete(_redis_pending_key(chat_key))
            else:
                r.set(_redis_pending_key(chat_key), json.dumps(payload, ensure_ascii=False))
            return
        except Exception as e:
            print(f"❌ Redis save_pending_add failed: {e}")

    data = load_watchlist()
    if "__pending__" not in data or not isinstance(data.get("__pending__"), dict):
        data["__pending__"] = {}
    if payload is None:
        data["__pending__"].pop(chat_key, None)
    else:
        data["__pending__"][chat_key] = payload
    save_watchlist(data)

def parse_periodicidad_horas_from_text(text: str) -> float | None:
    """
    Acepta entradas como:
    - "6"
    - "cada 12 horas"
    - "diario" / "diaria" (24)
    - "cada día" (24)
    - "semanal" (168)
    """
    t = (text or "").strip().lower()
    if not t:
        return None

    if t.isdigit():
        n = float(t)
        return n if n > 0 else None

    if "diar" in t or "cada día" in t or "cada dia" in t:
        return 24.0
    if "seman" in t:
        return 24.0 * 7

    import re
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(h|hora|horas)\b", t)
    if m:
        val = float(m.group(1).replace(",", "."))
        return val if val > 0 else None

    m = re.search(r"cada\s+(\d+(?:[.,]\d+)?)\b", t)
    if m:
        val = float(m.group(1).replace(",", "."))
        return val if val > 0 else None

    return None

def parse_periodicidad_map_from_text(text: str) -> dict[int, float] | None:
    """
    Parse inputs tipo:
      "1:12 2:24"
      "1=6h, 2=12h"
      "1: diario 2: semanal"
    Devuelve {1: 12.0, 2: 24.0} o None si no aplica.
    """
    import re
    t = (text or "").strip().lower()
    if not t:
        return None

    pairs = re.findall(r"(\d+)\s*[:=]\s*([^,;\n]+)", t)
    if not pairs:
        return None

    out: dict[int, float] = {}
    for idx_s, val_s in pairs:
        idx = int(idx_s)
        h = parse_periodicidad_horas_from_text(val_s.strip())
        if h:
            out[idx] = h
    return out or None

def _looks_like_list_message(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if len(lines) >= 3:
        return True
    # heurística: muchos separadores tipo "-" / "•" / numeración
    bulletish = sum(1 for ln in lines if ln[:2] in ("- ", "* ") or ln.startswith("•") or ln[:2].isdigit())
    return bulletish >= 2

def parse_batch_items_with_claude(message: str) -> list[dict]:
    """
    Extrae ítems de un listado. Cada item debe traer al menos "producto".
    Opcionalmente: url, precio_objetivo, query_busqueda.
    """
    client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))
    prompt = f"""El usuario envió un listado de cosas para trackear (posible multi-línea):
\"\"\"{message}\"\"\"

Extrae items. Responde SOLO JSON array (sin markdown):
[
  {{
    "producto": "texto corto del producto con atributos (talle/color)",
    "precio_objetivo": null o número en euros si aparece,
    "url": null o string si aparece,
    "query_busqueda": "query optimizada para buscar el mejor precio"
  }}
]

Reglas:
- Un item por línea/entrada del usuario (si es posible).
- Si hay URL pegada sin https, normalízala agregando https://
- Si no hay precio objetivo, usa null.
- No inventes productos.
"""
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}]
    )
    text = resp.content[0].text.strip().strip("```json").strip("```").strip()
    try:
        arr = json.loads(text)
        if not isinstance(arr, list):
            return []
        cleaned = []
        for it in arr:
            if not isinstance(it, dict):
                continue
            producto = (it.get("producto") or "").strip()
            if not producto:
                continue
            cleaned.append({
                "producto": producto,
                "precio_objetivo": it.get("precio_objetivo"),
                "url": it.get("url"),
                "query_busqueda": it.get("query_busqueda") or producto,
            })
        return cleaned
    except Exception:
        return []

def _strip_tracking_params_in_text(text: str) -> str:
    # Reduce URLs gigantes (utm/gclid/etc) en el texto del usuario.
    out = []
    for tok in (text or "").split():
        if tok.startswith("http://") or tok.startswith("https://"):
            try:
                parts = urlsplit(tok.strip())
                out.append(urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))
            except Exception:
                out.append(tok)
        else:
            out.append(tok)
    return " ".join(out).strip()

def _basic_command_intent(text: str) -> str | None:
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in ("listar", "/listar", "lista", "ver", "ver lista"):
        return "listar"
    if t in ("help", "/help", "ayuda", "/start", "start"):
        return "ayuda"
    # comprado/eliminar con número
    if re.match(r"^(comprado|eliminar)\s+\d+\s*$", t):
        return t.split()[0]
    return None

def normalize_user_message(text: str) -> dict:
    """
    Capa previa: intenta entender el mensaje SIN LLM.
    Devuelve:
      {
        "kind": "command"|"batch"|"single"|"empty",
        "command": "listar"|"ayuda"|"comprado"|"eliminar"|None,
        "number": int|None,
        "clean_text": str,
      }
    """
    raw = (text or "").strip()
    if not raw:
        return {"kind": "empty", "command": None, "number": None, "clean_text": ""}

    clean = _strip_tracking_params_in_text(raw)
    cmd = _basic_command_intent(clean)
    if cmd in ("comprado", "eliminar"):
        n = int(clean.split()[1])
        return {"kind": "command", "command": cmd, "number": n, "clean_text": clean}
    if cmd:
        return {"kind": "command", "command": cmd, "number": None, "clean_text": clean}

    if _looks_like_list_message(clean):
        return {"kind": "batch", "command": None, "number": None, "clean_text": clean}

    return {"kind": "single", "command": None, "number": None, "clean_text": clean}

def coerce_llm_intent(intent: dict, fallback_text: str) -> dict:
    """
    Valida/normaliza salida del LLM para evitar estados raros.
    """
    if not isinstance(intent, dict):
        return {"accion": "desconocido"}
    accion = intent.get("accion") if intent.get("accion") in ("agregar", "eliminar", "listar", "comprado", "desconocido") else "desconocido"
    out = {"accion": accion}
    if accion == "agregar":
        producto = (intent.get("producto") or fallback_text or "").strip()
        out["producto"] = producto
        out["precio_objetivo"] = intent.get("precio_objetivo")
        out["numero_item"] = intent.get("numero_item")
        out["query_busqueda"] = (intent.get("query_busqueda") or producto).strip()
        out["periodicidad_horas"] = intent.get("periodicidad_horas")
    else:
        out["numero_item"] = intent.get("numero_item")
    return out

def load_pending_batch(chat_key: str) -> dict | None:
    r = get_redis()
    if r is not None:
        try:
            raw = r.get(_redis_pending_batch_key(chat_key))
            if not raw:
                return None
            val = json.loads(raw)
            return val if isinstance(val, dict) else None
        except Exception as e:
            print(f"❌ Redis load_pending_batch failed: {e}")
            return None

    data = load_watchlist()
    pending = None
    if isinstance(data.get("__pending_batch__"), dict):
        pending = data["__pending_batch__"].get(chat_key)
    return pending if isinstance(pending, dict) else None

def save_pending_batch(chat_key: str, payload: dict | None):
    r = get_redis()
    if r is not None:
        try:
            if payload is None:
                r.delete(_redis_pending_batch_key(chat_key))
            else:
                r.set(_redis_pending_batch_key(chat_key), json.dumps(payload, ensure_ascii=False))
            return
        except Exception as e:
            print(f"❌ Redis save_pending_batch failed: {e}")

    data = load_watchlist()
    if "__pending_batch__" not in data or not isinstance(data.get("__pending_batch__"), dict):
        data["__pending_batch__"] = {}
    if payload is None:
        data["__pending_batch__"].pop(chat_key, None)
    else:
        data["__pending_batch__"][chat_key] = payload
    save_watchlist(data)

# ── Claude: parsear intención ─────────────────────────────────────────────────

def parse_user_intent(message: str) -> dict:
    client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))
    prompt = f"""El usuario mandó este mensaje a un agente de price tracking por WhatsApp:
"{message}"

Responde SOLO con JSON (sin markdown):
{{
  "accion": "agregar" | "eliminar" | "listar" | "comprado" | "desconocido",
  "producto": "nombre del producto con todos los detalles relevantes",
  "precio_objetivo": null o número en euros,
  "numero_item": null o número (si dice 'comprado 2' o 'eliminar 3'),
  "query_busqueda": "query optimizada para buscar precio en Google Shopping",
  "periodicidad_horas": null o número (si el usuario dice 'cada 12 horas', 'revisar diario', etc.; default null)
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

    client = TavilyClient(api_key=require_env("TAVILY_API_KEY"))
    # Búsqueda web general: no restringimos a tiendas concretas.
    # Usamos variaciones ligeras para aumentar recall sin sesgar a dominios.
    queries = [
        query,
        f"{producto} precio",
        f"{query} comprar",
        f"{query} oferta precio",
    ]

    seen, results = set(), []
    for q in queries:
        try:
            resp = client.search(q, max_results=6) or {}
            for r in resp.get("results", []) or []:
                url = r.get("url")
                if url and url not in seen:
                    seen.add(url)
                    results.append(r)
        except Exception:
            pass

    return results

def extract_prices_with_claude(producto: str, raw_results: list) -> list:
    if not raw_results:
        return []

    client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))
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

# ── Mensajería ────────────────────────────────────────────────────────────────

def send_message(chat_key: str, body: str):
    # chat_key puede ser "default" por migración de formato antiguo
    if chat_key == "default":
        return
    telegram_send(chat_id=int(chat_key), text=body)

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
    all_data = load_watchlist()
    if not all_data:
        print("   Watchlist vacía.")
        return

    now = datetime.datetime.now().isoformat()
    now_dt = datetime.datetime.now(datetime.timezone.utc)

    for chat_key, chat_watchlist in all_data.items():
        if not isinstance(chat_watchlist, dict):
            continue

        active = {k: v for k, v in chat_watchlist.items() if isinstance(v, dict) and v.get("activo", True)}
        if not active:
            continue

        for item_id, item in active.items():
            print(f"  → ({chat_key}) {item['producto']}")

            # Respetar periodicidad por item (si está definida)
            try:
                periodicidad_h = item.get("periodicidad_horas")
                if periodicidad_h:
                    last_iso = item.get("ultima_revision")
                    if last_iso:
                        last_dt = datetime.datetime.fromisoformat(last_iso)
                        # si no tiene tz, asumimos UTC
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)
                        due_at = last_dt + datetime.timedelta(hours=float(periodicidad_h))
                        if now_dt < due_at:
                            continue
            except Exception:
                # Si falla el parseo, no bloqueamos el chequeo.
                pass

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
                    f"🔔 Bajada de precio!\n\n"
                    f"🛍️ {item['producto']}\n"
                    f"💰 €{nuevo_precio:.2f} (antes €{prev_precio:.2f})\n"
                    f"📉 Bajó €{bajada:.2f} ({pct:.1f}%)\n"
                    f"🏪 {best['tienda']}\n"
                    f"🔗 {best['url']}\n\n"
                    f"Responde: comprado {item_num}"
                )
                send_message(chat_key, msg)
                print(f"    📱 Alerta enviada!")

            # Alerta si alcanzó precio objetivo
            if item.get("precio_objetivo") and nuevo_precio <= item["precio_objetivo"]:
                msg = (
                    f"🎯 Precio objetivo alcanzado!\n\n"
                    f"🛍️ {item['producto']}\n"
                    f"💰 €{nuevo_precio:.2f} (tu objetivo: €{item['precio_objetivo']})\n"
                    f"🏪 {best['tienda']}\n"
                    f"🔗 {best['url']}"
                )
                send_message(chat_key, msg)

            chat_watchlist[item_id] = item

        all_data[chat_key] = chat_watchlist

    save_watchlist(all_data)
    print(f"   ✅ Revisión completada.")

def check_single_async(chat_key: str, item_id: str):
    """Busca precio inicial de un producto recién agregado."""
    def _run():
        import time; time.sleep(3)
        chat_watchlist = load_chat_watchlist(chat_key)
        if item_id not in chat_watchlist:
            return
        item = chat_watchlist[item_id]
        raw = search_prices(item["producto"], item.get("query_busqueda", item["producto"]))
        prices = extract_prices_with_claude(item["producto"], raw)
        if prices:
            best = prices[0]
            item["mejor_precio_actual"] = best["precio"]
            item["mejor_tienda"] = best["tienda"]
            item["mejor_url"] = best["url"]
            item["historial"] = [{"fecha": datetime.datetime.now().isoformat(), "precio": best["precio"], "tienda": best["tienda"], "url": best["url"]}]
            chat_watchlist[item_id] = item
            save_chat_watchlist(chat_key, chat_watchlist)
            send_message(
                chat_key,
                f"✅ Precio inicial encontrado!\n\n"
                f"🛍️ {item['producto']}\n"
                f"💰 Mejor precio ahora: €{best['precio']:.2f}\n"
                f"🏪 {best['tienda']}\n"
                f"🔗 {best['url']}\n\n"
                f"Te aviso cuando baje 🔔"
            )
    threading.Thread(target=_run, daemon=True).start()

# ── Flask webhook ─────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    if chat_id is None:
        return "", 204

    chat_key = str(chat_id)
    print(f"📩 (tg:{chat_key}) {text}")

    if not text:
        telegram_send(chat_id, "Dime qué quieres comprar, o escribe: listar")
        return "", 204

    # 1) Normalización previa (sin LLM)
    norm = normalize_user_message(text)
    clean_text = norm["clean_text"]

    # Si hay un batch pendiente, este mensaje es parte del flujo (confirmación / periodicidad)
    pending_batch = load_pending_batch(chat_key)
    if pending_batch is not None:
        step = pending_batch.get("step")
        items = pending_batch.get("items") if isinstance(pending_batch.get("items"), list) else []

        def _render_items(items_: list[dict]) -> str:
            lines = []
            for i, it in enumerate(items_, 1):
                p = it.get("producto")
                pr = it.get("precio_objetivo")
                u = it.get("url")
                extra = []
                if pr is not None:
                    extra.append(f"€{pr}")
                if u:
                    extra.append(str(u))
                tail = (" — " + " | ".join(extra)) if extra else ""
                lines.append(f"{i}. {p}{tail}")
            return "\n".join(lines)

        low = clean_text.strip().lower()
        if step == "await_confirm":
            if low in ("si", "sí", "s", "ok", "dale", "confirmo", "confirmar"):
                pending_batch["step"] = "await_periodicity"
                save_pending_batch(chat_key, pending_batch)
                telegram_send(
                    chat_id,
                    "Perfecto. ¿Cada cuántas horas reviso *todos*?\n"
                    "Responde por ejemplo: 12 (o 'diario').\n\n"
                    "Si quieres distinto por producto: responde `1:12h 2:24h 3:6h`."
                )
                return "", 204
            if low in ("no", "n", "cancelar", "cancela"):
                save_pending_batch(chat_key, None)
                telegram_send(chat_id, "Listo, cancelé el listado.")
                return "", 204

            telegram_send(
                chat_id,
                "¿Confirmas que agregue este listado? Responde **sí** o **no**.\n\n" + _render_items(items)
            )
            return "", 204

        if step == "await_periodicity":
            per_map = parse_periodicidad_map_from_text(clean_text)
            per_all = None if per_map else parse_periodicidad_horas_from_text(clean_text)
            if not per_map and not per_all:
                telegram_send(
                    chat_id,
                    "No entendí la periodicidad. Ejemplos: 12, 24 (diario), o `1:12h 2:24h`."
                )
                return "", 204

            chat_watchlist = load_chat_watchlist(chat_key)
            created_ids = []
            for i, it in enumerate(items, 1):
                producto = (it.get("producto") or "").strip()
                if not producto:
                    continue
                periodicidad_horas = (per_map.get(i) if per_map else per_all)
                item_id = f"{producto[:30].lower().replace(' ', '-')}-{datetime.date.today().isoformat()}-{i}"
                chat_watchlist[item_id] = {
                    "producto": producto,
                    "query_busqueda": it.get("query_busqueda") or producto,
                    "precio_objetivo": it.get("precio_objetivo"),
                    "periodicidad_horas": periodicidad_horas,
                    "mejor_precio_actual": None,
                    "activo": True,
                    "agregado": datetime.datetime.now().isoformat(),
                    "historial": []
                }
                created_ids.append(item_id)

            save_chat_watchlist(chat_key, chat_watchlist)
            save_pending_batch(chat_key, None)

            telegram_send(
                chat_id,
                f"✅ Listo! Agregué {len(created_ids)} productos.\n\n"
                "Buscando precio inicial (puede tardar un poco)..."
            )
            # Dispara búsquedas iniciales (en background)
            for iid in created_ids[:10]:
                check_single_async(chat_key, iid)
            return "", 204

    # Si hay un alta pendiente, esta respuesta se interpreta como periodicidad
    pending = load_pending_add(chat_key)
    if pending is not None:
        periodicidad_h = parse_periodicidad_horas_from_text(clean_text)
        if not periodicidad_h:
            telegram_send(
                chat_id,
                "¿Cada cuántas horas quieres que lo revise? Ejemplos: 6, 12, 24 (diario)."
            )
            return "", 204

        # Completa el alta pendiente
        producto = pending.get("producto")
        query = pending.get("query_busqueda") or producto
        precio_objetivo = pending.get("precio_objetivo")
        if not producto:
            save_pending_add(chat_key, None)
            telegram_send(chat_id, "Se perdió el producto pendiente. Repite el mensaje con el producto.")
            return "", 204

        item_id = f"{str(producto)[:30].lower().replace(' ', '-')}-{datetime.date.today().isoformat()}"
        chat_watchlist = load_chat_watchlist(chat_key)
        chat_watchlist[item_id] = {
            "producto": producto,
            "query_busqueda": query,
            "precio_objetivo": precio_objetivo,
            "periodicidad_horas": periodicidad_h,
            "mejor_precio_actual": None,
            "activo": True,
            "agregado": datetime.datetime.now().isoformat(),
            "historial": []
        }
        save_chat_watchlist(chat_key, chat_watchlist)
        save_pending_add(chat_key, None)

        obj_str = f"\n🎯 Precio objetivo: €{precio_objetivo}" if precio_objetivo else ""
        reply = (
            f"✅ Listo! Lo voy a revisar cada {periodicidad_h:g}h.\n\n"
            f"🛍️ {producto}{obj_str}\n\n"
            "Buscando el mejor precio ahora, te aviso en un momento..."
        )
        telegram_send(chat_id, reply)
        check_single_async(chat_key, item_id)
        return "", 204

    # 2) Comandos simples (sin LLM)
    if norm["kind"] == "command":
        cmd = norm["command"]
        chat_watchlist = load_chat_watchlist(chat_key)
        if cmd == "listar":
            telegram_send(chat_id, format_watchlist(chat_watchlist))
            return "", 204
        if cmd in ("comprado", "eliminar"):
            num = norm["number"]
            active_keys = [k for k, v in chat_watchlist.items() if v.get("activo", True)]
            if num and 1 <= num <= len(active_keys):
                iid = active_keys[num - 1]
                nombre = chat_watchlist[iid]["producto"]
                chat_watchlist[iid]["activo"] = False
                chat_watchlist[iid]["fecha_compra"] = datetime.date.today().isoformat()
                save_chat_watchlist(chat_key, chat_watchlist)
                telegram_send(chat_id, f"🎉 Listo! Dejé de trackear:\n*{nombre}*\n\n¿Qué más quieres buscar?")
            else:
                telegram_send(chat_id, "⚠️ Dime el número. Escribe *listar* para ver tus productos.")
            return "", 204
        if cmd == "ayuda":
            telegram_send(
                chat_id,
                "Puedes decirme:\n\n"
                "- quiero comprar [producto]\n"
                "- listar\n"
                "- comprado N\n"
                "- eliminar N\n\n"
                "Si mandas un listado (varias líneas), lo agrego en lote."
            )
            return "", 204

    # 3) Batch detection (sin LLM para la intención; sí LLM para extraer items)
    if norm["kind"] == "batch":
        items = parse_batch_items_with_claude(clean_text)
        if not items:
            telegram_send(chat_id, "No pude extraer los productos del listado. ¿Puedes enviarlos uno por línea?")
            return "", 204
        save_pending_add(chat_key, None)
        save_pending_batch(chat_key, {
            "step": "await_confirm",
            "items": items,
            "created_at": datetime.datetime.now().isoformat(),
        })
        telegram_send(
            chat_id,
            "Detecté este listado. ¿Confirmas que lo agregue? Responde **sí** o **no**.\n\n" +
            "\n".join([f"{i+1}. {it['producto']}" for i, it in enumerate(items)])
        )
        return "", 204

    # 4) Single: usamos LLM para parsear intención
    try:
        chat_watchlist = load_chat_watchlist(chat_key)
        intent = coerce_llm_intent(parse_user_intent(clean_text), clean_text)
        accion = intent.get("accion", "desconocido")
    except Exception as e:
        print(f"❌ Error inicializando request: {e}")
        try:
            telegram_send(
                chat_id,
                "⚠️ No puedo procesar tu mensaje ahora.\n"
                "Revisa variables: ANTHROPIC_API_KEY, TAVILY_API_KEY, TELEGRAM_BOT_TOKEN."
            )
        except Exception as send_err:
            print(f"❌ También falló enviar Telegram: {send_err}")
        return "", 204

    if accion == "agregar":
        producto = intent.get("producto", clean_text)
        query = intent.get("query_busqueda", producto)
        precio_objetivo = intent.get("precio_objetivo")
        periodicidad_horas = intent.get("periodicidad_horas")

        if not periodicidad_horas:
            # No guardamos aún; pedimos periodicidad y guardamos el "pendiente"
            save_pending_add(chat_key, {
                "producto": producto,
                "query_busqueda": query,
                "precio_objetivo": precio_objetivo,
            })
            telegram_send(
                chat_id,
                "¿Cada cuántas horas quieres que lo revise?\n"
                "Responde solo con un número (ej: 6, 12, 24) o 'diario'."
            )
            return "", 204

        item_id = f"{producto[:30].lower().replace(' ', '-')}-{datetime.date.today().isoformat()}"
        chat_watchlist[item_id] = {
            "producto": producto,
            "query_busqueda": query,
            "precio_objetivo": precio_objetivo,
            "periodicidad_horas": periodicidad_horas,
            "mejor_precio_actual": None,
            "activo": True,
            "agregado": datetime.datetime.now().isoformat(),
            "historial": []
        }
        save_chat_watchlist(chat_key, chat_watchlist)
        obj_str = f"\n🎯 Precio objetivo: €{precio_objetivo}" if precio_objetivo else ""
        reply = f"👀 Agregado al tracker!\n\n🛍️ {producto}{obj_str}\n\nBuscando el mejor precio ahora, te aviso en un momento..."
        check_single_async(chat_key, item_id)

    elif accion == "listar":
        reply = format_watchlist(chat_watchlist)

    elif accion in ("comprado", "eliminar"):
        num = intent.get("numero_item")
        active_keys = [k for k, v in chat_watchlist.items() if v.get("activo", True)]
        if num and 1 <= num <= len(active_keys):
            iid = active_keys[num - 1]
            nombre = chat_watchlist[iid]["producto"]
            chat_watchlist[iid]["activo"] = False
            chat_watchlist[iid]["fecha_compra"] = datetime.date.today().isoformat()
            save_chat_watchlist(chat_key, chat_watchlist)
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

    telegram_send(chat_id, reply)
    return "", 204

@app.route("/health", methods=["GET"])
def health():
    data = load_watchlist()
    active = 0
    for _, chat_watchlist in data.items():
        if isinstance(chat_watchlist, dict):
            active += sum(1 for v in chat_watchlist.values() if isinstance(v, dict) and v.get("activo", True))
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
