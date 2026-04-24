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
from urllib.parse import urlparse
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import JobLookupError
try:
    from scrapegraph_py import Client as ScrapeGraphClient
except Exception:
    ScrapeGraphClient = None

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

def _redis_settings_key(chat_key: str) -> str:
    return f"settings:{chat_key}"

def _redis_pending_location_key(chat_key: str) -> str:
    return f"pending_location:{chat_key}"

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

def load_chat_settings(chat_key: str) -> dict:
    """
    Preferencias por chat (ej. ubicación de búsqueda).
    """
    r = get_redis()
    if r is not None:
        try:
            raw = r.get(_redis_settings_key(chat_key))
            if not raw:
                return {}
            val = json.loads(raw)
            return val if isinstance(val, dict) else {}
        except Exception as e:
            print(f"❌ Redis load_chat_settings failed: {e}")
            return {}

    data = load_watchlist()
    settings_root = data.get("__settings__") if isinstance(data.get("__settings__"), dict) else {}
    val = settings_root.get(chat_key) if isinstance(settings_root, dict) else None
    return val if isinstance(val, dict) else {}

def save_chat_settings(chat_key: str, settings: dict):
    r = get_redis()
    if r is not None:
        try:
            r.set(_redis_settings_key(chat_key), json.dumps(settings or {}, ensure_ascii=False))
            return
        except Exception as e:
            print(f"❌ Redis save_chat_settings failed: {e}")

    data = load_watchlist()
    if "__settings__" not in data or not isinstance(data.get("__settings__"), dict):
        data["__settings__"] = {}
    data["__settings__"][chat_key] = settings or {}
    save_watchlist(data)

def load_pending_location(chat_key: str) -> dict | None:
    r = get_redis()
    if r is not None:
        try:
            raw = r.get(_redis_pending_location_key(chat_key))
            if not raw:
                return None
            val = json.loads(raw)
            return val if isinstance(val, dict) else None
        except Exception as e:
            print(f"❌ Redis load_pending_location failed: {e}")
            return None

    data = load_watchlist()
    pending_root = data.get("__pending_location__") if isinstance(data.get("__pending_location__"), dict) else {}
    val = pending_root.get(chat_key) if isinstance(pending_root, dict) else None
    return val if isinstance(val, dict) else None

def save_pending_location(chat_key: str, payload: dict | None):
    r = get_redis()
    if r is not None:
        try:
            if payload is None:
                r.delete(_redis_pending_location_key(chat_key))
            else:
                r.set(_redis_pending_location_key(chat_key), json.dumps(payload, ensure_ascii=False))
            return
        except Exception as e:
            print(f"❌ Redis save_pending_location failed: {e}")

    data = load_watchlist()
    if "__pending_location__" not in data or not isinstance(data.get("__pending_location__"), dict):
        data["__pending_location__"] = {}
    if payload is None:
        data["__pending_location__"].pop(chat_key, None)
    else:
        data["__pending_location__"][chat_key] = payload
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
    # Reduce URLs gigantes (utm/gclid/etc) sin romper saltos de línea.
    t = (text or "")

    def _repl(m):
        url = m.group(0)
        try:
            parts = urlsplit(url.strip())
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except Exception:
            return url

    # Reemplaza solo URLs http(s)://... manteniendo el resto del texto intacto.
    t = re.sub(r"https?://\S+", _repl, t)
    return t.strip()

def _basic_command_intent(text: str) -> str | None:
    t = (text or "").strip().lower()
    if not t:
        return None
    # Saludos / small talk (para responder más "humano")
    if re.match(r"^(hola|buenas|buenos dias|buen día|buenas tardes|buenas noches|hey|que tal|qué tal)\b", t):
        return "greeting"

    # Listar (sinónimos)
    if t in ("listar", "/listar", "lista", "ver", "ver lista", "mostrame", "muéstrame", "mostrame la lista", "mostrame mi lista", "mostrar", "mostrar lista"):
        return "listar"

    # Cambiar schedule/sync (sinónimos)
    if "cambiar" in t and ("schedule" in t or "sincron" in t or "frecuencia" in t or "cada" in t):
        return "sync_jobs"
    if t.startswith("schedule ") or t.startswith("frecuencia "):
        return "sync_jobs"

    if t in ("help", "/help", "ayuda", "/start", "start"):
        return "ayuda"
    if t in ("forzar busqueda", "forzar búsqueda", "buscar ahora", "revisar ahora", "forzar", "forzarbusqueda"):
        return "forzar_busqueda"
    if t.startswith("sincronizacion") or t.startswith("sincronización") or t.startswith("sync "):
        return "sync_jobs"
    # comprado/eliminar con número
    if re.match(r"^(comprado|eliminar)\s+[\d,\s]+\s*$", t):
        return t.split()[0]
    if t.startswith("ubicacion ") or t.startswith("ubicación "):
        return "ubicacion"
    return None

def build_intro_message(settings: dict | None = None) -> str:
    loc = (settings or {}).get("search_location")
    loc_line = f"📍 Busco precios en: {loc}\n\n" if loc else ""
    return (
        "Hola, soy Lola.\n"
        "Me especializo en encontrar el mejor precio (nuevo, no usado) y avisarte cuando conviene comprar.\n"
        "Si me das el producto y tu presupuesto, me encargo del resto.\n\n"
        f"{loc_line}"
        "¿Qué hacemos?\n"
        "- ¿Querés agregar algo para que lo trackee?\n"
        "- ¿O querés que te muestre tu lista?\n\n"
        "Atajos (si te gusta escribir directo):\n"
        "- `listar`\n"
        "- `forzar busqueda`\n"
        "- `sincronizacion 2` (cada 2 horas)\n"
        "- `ubicacion España`\n\n"
        "Ejemplo:\n"
        "“Quiero comprar Dr Martens Reeder talla 42 por menos de 140€”\n"
    )

def build_greeting_message(settings: dict | None = None) -> str:
    loc = (settings or {}).get("search_location")
    loc_line = f"📍 Ahora mismo estoy buscando en: {loc}\n\n" if loc else ""
    return (
        "Hola! Qué gusto leerte.\n"
        "¿Qué estás buscando hoy?\n"
        "Si me pasás el producto (y si tenés, tu precio objetivo), te lo trackeo.\n\n"
        f"{loc_line}"
        "Si querés, podés responder con una de estas:\n"
        "- “mostrame”\n"
        "- “forzar busqueda”\n"
        "- “quiero cambiar el schedule”\n"
    )

def parse_item_numbers_from_text(text: str) -> list[int] | None:
    """
    Acepta:
      - "2"
      - "2,3,4"
      - "2 3 4"
      - "2, 3, 4"
    Devuelve lista única y ordenada.
    """
    t = (text or "").strip()
    if not t:
        return None
    # Extrae todos los enteros en el texto
    nums = [int(x) for x in re.findall(r"\d+", t)]
    if not nums:
        return None
    # Dedup + sort manteniendo orden de aparición
    seen = set()
    out = []
    for n in nums:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out

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
        nums = parse_item_numbers_from_text(clean)
        # compat: si por alguna razón no parsea, no tratamos como comando válido
        return {"kind": "command", "command": cmd, "numbers": nums, "clean_text": clean}
    if cmd:
        if cmd == "ubicacion":
            loc = clean.split(" ", 1)[1].strip() if " " in clean else ""
            return {"kind": "command", "command": cmd, "number": None, "clean_text": clean, "location": loc}
        if cmd == "sync_jobs":
            arg = clean.split(" ", 1)[1].strip() if " " in clean else ""
            return {"kind": "command", "command": cmd, "number": None, "clean_text": clean, "sync_arg": arg}
        if cmd == "greeting":
            return {"kind": "command", "command": cmd, "number": None, "clean_text": clean}
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

def route_intent_with_claude(message: str) -> dict | None:
    """
    Router LLM: clasifica el mensaje en intents cerrados para ejecutar comandos
    sin depender de strings exactos.

    Retorna dict con:
      {
        "intent": one of [
          "greeting","listar","comprado","eliminar","forzar_busqueda",
          "ubicacion","sync_jobs","agregar_single","agregar_batch","out_of_scope"
        ],
        "numbers": [int] | null,
        "location": str | null,
        "periodicidad_horas": number | null
      }
    """
    if os.getenv("USE_LLM_ROUTER", "1") != "1":
        return None
    msg = (message or "").strip()
    if not msg:
        return None

    # evita prompts gigantes
    msg = msg[:1200]

    client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))
    prompt = f"""Eres un router de intents para un bot de tracking de precios.

Mensaje del usuario:
\"\"\"{msg}\"\"\"

Devuelve SOLO JSON (sin markdown) con este schema:
{{
  "intent": "greeting"|"listar"|"comprado"|"eliminar"|"forzar_busqueda"|"ubicacion"|"sync_jobs"|"agregar_single"|"agregar_batch"|"out_of_scope",
  "numbers": null o array de enteros (para comprado/eliminar),
  "location": null o string (para ubicacion),
  "periodicidad_horas": null o número (para sync_jobs; ej diario=24)
}}

Reglas:
- Si es saludo: greeting.
- Si pide ver lo que tiene / mostrar lista: listar.
- Si dice que compró/eliminar varios: comprado/eliminar con numbers.
- Si pide revisar ahora: forzar_busqueda.
- Si quiere cambiar frecuencia/schedule/sincronización: sync_jobs (si no da número, periodicidad_horas=null).
- Si manda un listado de varios productos (multi-línea): agregar_batch.
- Si quiere agregar un solo producto: agregar_single.
- Si no encaja: out_of_scope.
"""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=250,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip().strip("```json").strip("```").strip()
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        intent = data.get("intent")
        allowed = {
            "greeting","listar","comprado","eliminar","forzar_busqueda",
            "ubicacion","sync_jobs","agregar_single","agregar_batch","out_of_scope"
        }
        if intent not in allowed:
            return None
        # normaliza fields
        if "numbers" in data and data["numbers"] is not None:
            if isinstance(data["numbers"], list):
                nums = []
                for n in data["numbers"]:
                    if isinstance(n, int):
                        nums.append(n)
                    elif isinstance(n, float) and n.is_integer():
                        nums.append(int(n))
                data["numbers"] = nums or None
            else:
                data["numbers"] = None
        if "periodicidad_horas" in data and data["periodicidad_horas"] is not None:
            try:
                data["periodicidad_horas"] = float(data["periodicidad_horas"])
            except Exception:
                data["periodicidad_horas"] = None
        if "location" in data and data["location"] is not None and not isinstance(data["location"], str):
            data["location"] = None
        return data
    except Exception:
        return None

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

def search_prices(producto: str, query: str, location: str | None = None) -> list:
    try:
        from tavily import TavilyClient
    except ImportError:
        return []

    client = TavilyClient(api_key=require_env("TAVILY_API_KEY"))
    # Búsqueda web general: no restringimos a tiendas concretas.
    # Usamos variaciones ligeras para aumentar recall sin sesgar a dominios.
    # Ubicación (si existe) para sesgar resultados a país/ciudad/region.
    # Se agrega como término, sin forzar dominios.
    loc = (location or "").strip()
    loc_suffix = f" {loc}" if loc else ""

    max_q = int(os.getenv("TAVILY_QUERY_MAX_CHARS", "400"))

    def _cap(s: str) -> str:
        s = (s or "").strip()
        if len(s) <= max_q:
            return s
        # corta sin romper palabras demasiado
        cut = s.rfind(" ", 0, max_q)
        if cut < 80:
            cut = max_q
        return s[:cut].strip()

    # Evitar resultados usados/reacondicionados por defecto
    exclude_used = os.getenv("EXCLUDE_USED_RESULTS", "1") == "1"
    neg = ""
    if exclude_used:
        # Tavily usa motores web: estos términos ayudan a filtrar marketplaces/2da mano.
        neg = " -usado -usada -segunda_mano -\"segunda mano\" -reacondicionado -refurbished -refurb -wallapop -ebay -vinted -milanuncios -todocoleccion -vintage -antiguo -antigua -lote"

    queries = [
        _cap(f"{query}{loc_suffix}{neg}"),
        _cap(f"{producto} precio{loc_suffix}{neg}"),
        _cap(f"{query} comprar{loc_suffix}{neg}"),
        _cap(f"{query} oferta precio{loc_suffix}{neg}"),
    ]

    seen, results = set(), []
    for q in queries:
        if not q:
            continue
        try:
            resp = client.search(q, max_results=6) or {}
            if os.getenv("DEBUG_TAVILY") == "1":
                res = resp.get("results", []) or []
                n = len(res)
                print(f"🔎 Tavily query ({n} results): {q[:160]}")
                if exclude_used:
                    kept = [r for r in res if not looks_used_listing(r.get("title"), r.get("content"), r.get("url"))]
                    print(f"   filtered_used: kept {len(kept)}/{len(res)}")
                    res_to_print = kept
                else:
                    res_to_print = res
                for i, r in enumerate(res_to_print[:3], 1):
                    url = (r.get("url") or "").strip()
                    title = (r.get("title") or "").strip()
                    try:
                        host = urlsplit(url).netloc
                    except Exception:
                        host = ""
                    print(f"   {i}. {title[:90]} ({host})")
                    print(f"      {url[:200]}")
            for r in resp.get("results", []) or []:
                url = r.get("url")
                if exclude_used and looks_used_listing(r.get("title"), r.get("content"), url):
                    continue
                if url and url not in seen:
                    seen.add(url)
                    results.append(r)
        except Exception as e:
            if os.getenv("DEBUG_TAVILY") == "1":
                print(f"❌ Tavily search error for query: {q[:160]}")
                print(f"   {type(e).__name__}: {e}")

    return results

def extract_prices_with_claude(producto: str, raw_results: list) -> list:
    if not raw_results:
        return []

    client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))
    results_text = "\n\n".join([
        f"URL: {r.get('url')}\nTítulo: {r.get('title')}\nContenido: {r.get('content','')[:2000]}"
        for r in raw_results
    ])

    prompt = f"""Analiza resultados de búsqueda para: "{producto}"

{results_text}

Extrae listings reales con precio. Responde SOLO JSON array (sin markdown):
[{{"tienda":"Amazon","precio":89.99,"moneda":"EUR","url":"https://...","descripcion":"nombre producto","disponible":true}}]

Reglas:
- Excluye productos usados/segunda mano/reacondicionados (used, pre-owned, refurbished, reacondicionado, segunda mano).
- Solo precios numéricos claros. Ordena menor a mayor. Si no hay, devuelve []."""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    text = resp.content[0].text.strip().strip("```json").strip("```").strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []

def _domain_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc or ""
    except Exception:
        return ""

USED_KEYWORDS = (
    "usado", "usada", "segunda mano", "2da mano", "segundamano",
    "reacondicionado", "reacondicionada", "refurbished", "refurb",
    "pre-owned", "preowned", "reconditioned",
    "vintage", "antiguo", "antigua", "antiguos", "antiguas", "lote", "colección", "coleccion",
)

USED_DOMAINS = (
    "ebay.", "wallapop.", "vinted.", "milanuncios.", "facebook.com/marketplace",
    "todocoleccion.",
)

def looks_used_listing(title: str | None, content: str | None, url: str | None) -> bool:
    t = (title or "").lower()
    c = (content or "").lower()
    u = (url or "").lower()
    if any(d in u for d in USED_DOMAINS):
        return True
    blob = f"{t}\n{c}\n{u}"
    return any(k in blob for k in USED_KEYWORDS)

def _to_float_price(val) -> float | None:
    try:
        if isinstance(val, (int, float)):
            return float(val)
        if not isinstance(val, str):
            return None
        s = val.strip()
        s = s.replace("\u00a0", " ")
        # quita símbolos típicos
        s = s.replace("€", "").replace("EUR", "").strip()
        # soporta 1.234,56 o 1,234.56
        # si hay ambas, asumimos que la última separa decimales
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
        m = re.search(r"(\d+(?:\.\d+)?)", s)
        if not m:
            return None
        return float(m.group(1))
    except Exception:
        return None

def _extract_price_from_jsonld(obj) -> tuple[float | None, str | None]:
    # Devuelve (price, currency)
    if isinstance(obj, list):
        for x in obj:
            p, c = _extract_price_from_jsonld(x)
            if p is not None:
                return p, c
        return None, None

    if not isinstance(obj, dict):
        return None, None

    offers = obj.get("offers")
    if offers is not None:
        p, c = _extract_price_from_jsonld(offers)
        if p is not None:
            return p, c

    # offer directo
    price = obj.get("price") or obj.get("lowPrice")
    currency = obj.get("priceCurrency")
    p = _to_float_price(price)
    if p is not None:
        return p, currency

    # a veces está anidado
    for k in ("mainEntity", "itemOffered", "product", "data"):
        if k in obj:
            p, c = _extract_price_from_jsonld(obj.get(k))
            if p is not None:
                return p, c
    return None, None

def fetch_price_from_url(url: str) -> dict | None:
    """
    Intenta extraer precio desde HTML (JSON-LD / meta tags / itemprop).
    Devuelve un listing compatible con extract_prices_with_claude o None.
    """
    try:
        host = _domain_from_url(url).lower()
        # Por defecto no bloqueamos dominios; configura HTML_FETCH_SKIP_DOMAINS si quieres saltarte algunos.
        skip = os.getenv("HTML_FETCH_SKIP_DOMAINS", "").lower()
        skip_domains = [d.strip() for d in skip.split(",") if d.strip()]
        if any(host == d or host.endswith("." + d) for d in skip_domains):
            if os.getenv("DEBUG_HTML_FETCH") == "1" or os.getenv("DEBUG_TAVILY") == "1":
                print(f"⏭️  HTML fetch skipped for domain: {host}")
            return None

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; LolaBot/1.0; +https://github.com/dgarzond/lola-bot)",
            "Accept": "text/html,application/xhtml+xml",
        }

        # timeouts más agresivos para no bloquear el bot (connect, read)
        connect_timeout = float(os.getenv("HTML_FETCH_CONNECT_TIMEOUT_S", "3.0"))
        read_timeout = float(os.getenv("HTML_FETCH_READ_TIMEOUT_S", "8.0"))
        retries = int(os.getenv("HTML_FETCH_RETRIES", "1"))

        last_err = None
        for attempt in range(retries + 1):
            try:
                r = requests.get(url, headers=headers, timeout=(connect_timeout, read_timeout))
                break
            except requests.exceptions.ReadTimeout as e:
                last_err = e
                if os.getenv("DEBUG_HTML_FETCH") == "1" or os.getenv("DEBUG_TAVILY") == "1":
                    print(f"⏱️  HTML fetch ReadTimeout (attempt {attempt+1}/{retries+1}) for {host}")
            except requests.exceptions.ConnectTimeout as e:
                last_err = e
                if os.getenv("DEBUG_HTML_FETCH") == "1" or os.getenv("DEBUG_TAVILY") == "1":
                    print(f"⏱️  HTML fetch ConnectTimeout (attempt {attempt+1}/{retries+1}) for {host}")
            except requests.exceptions.RequestException as e:
                last_err = e
                break
        else:
            # no debería llegar acá
            return None

        if last_err is not None and 'r' not in locals():
            return None

        if r.status_code >= 400:
            return None

        html = r.text
        # Limita trabajo
        if len(html) > 1_200_000:
            html = html[:1_200_000]

        # 1) JSON-LD
        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
            raw = m.group(1).strip()
            if not raw:
                continue
            # algunos sitios ponen múltiples JSONs
            try:
                data = json.loads(raw)
            except Exception:
                continue
            price, currency = _extract_price_from_jsonld(data)
            if price is not None:
                return {
                    "tienda": _domain_from_url(url) or "Web",
                    "precio": float(price),
                    "moneda": currency or "EUR",
                    "url": url,
                    "descripcion": None,
                    "disponible": True,
                }

        # 2) Meta tags comunes
        meta_patterns = [
            r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+property=["\']og:price:amount["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+itemprop=["\']price["\'][^>]+content=["\']([^"\']+)["\']',
        ]
        for pat in meta_patterns:
            mm = re.search(pat, html, re.I)
            if mm:
                price = _to_float_price(mm.group(1))
                if price is not None:
                    return {
                        "tienda": _domain_from_url(url) or "Web",
                        "precio": float(price),
                        "moneda": "EUR",
                        "url": url,
                        "descripcion": None,
                        "disponible": True,
                    }

        return None
    except Exception as e:
        if os.getenv("DEBUG_TAVILY") == "1":
            print(f"❌ fetch_price_from_url failed: {type(e).__name__}: {e}")
        return None

def enrich_prices_from_html(producto: str, raw_results: list, existing: list) -> list:
    """
    Si Claude no encontró precios, intenta extraerlos directamente desde 1–3 URLs top.
    """
    if existing:
        return existing
    urls = []
    exclude_used = os.getenv("EXCLUDE_USED_RESULTS", "1") == "1"
    for r in raw_results[:5]:
        u = r.get("url")
        if u and isinstance(u, str):
            if exclude_used and looks_used_listing(r.get("title"), r.get("content"), u):
                continue
            urls.append(u)
    out = []
    seen = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        listing = fetch_price_from_url(u)
        if listing and isinstance(listing.get("precio"), (int, float)):
            out.append(listing)
    # Ordena por precio si hay
    out.sort(key=lambda x: x.get("precio", 10**12))
    return out

def scrapegraph_extract_listing(url: str) -> dict | None:
    """
    Fallback pesado: usa ScrapeGraphAI para extraer precio/moneda/condición.
    Requiere SCRAPEGRAPH_API_KEY.
    """
    if ScrapeGraphClient is None:
        return None
    api_key = os.getenv("SCRAPEGRAPH_API_KEY")
    if not api_key:
        return None

    try:
        client = ScrapeGraphClient(api_key=api_key)
        prompt = (
            "Extrae información del producto. Devuelve solo:\n"
            "- nombre (string)\n"
            "- precio (number)\n"
            "- moneda (string, idealmente EUR)\n"
            "- condicion (\"nuevo\"|\"usado\"|\"reacondicionado\"|null)\n"
            "- disponible (true/false/null)\n"
            "- notas (string corto)\n"
            "Si no hay precio claro, devuelve precio=null."
        )
        schema = {
            "type": "object",
            "properties": {
                "nombre": {"type": ["string", "null"]},
                "precio": {"type": ["number", "null"]},
                "moneda": {"type": ["string", "null"]},
                "condicion": {"type": ["string", "null"]},
                "disponible": {"type": ["boolean", "null"]},
                "notas": {"type": ["string", "null"]},
            },
        }
        resp = client.smartscraper(
            website_url=url,
            user_prompt=prompt,
            output_schema=schema,
        )
        data = resp if isinstance(resp, dict) else {}

        price = data.get("precio")
        price = float(price) if isinstance(price, (int, float)) else None
        currency = data.get("moneda") or "EUR"
        cond = (data.get("condicion") or "").lower()
        if cond in ("used", "usado", "segunda mano"):
            cond = "usado"
        if cond in ("refurbished", "reacondicionado", "refurb"):
            cond = "reacondicionado"
        if cond in ("new", "nuevo"):
            cond = "nuevo"
        cond = cond or None

        return {
            "tienda": _domain_from_url(url) or "Web",
            "precio": price,
            "moneda": currency,
            "url": url,
            "descripcion": data.get("nombre"),
            "disponible": data.get("disponible") if isinstance(data.get("disponible"), bool) else True,
            "condicion": cond,
            "notas": data.get("notas"),
        }
    except Exception as e:
        if os.getenv("DEBUG_SCRAPEGRAPH") == "1":
            print(f"❌ scrapegraph_extract_listing failed: {type(e).__name__}: {e}")
        return None

def claude_filter_listings(producto: str, listings: list[dict]) -> list[dict]:
    """
    Usa Claude para filtrar/normalizar listings (y excluir usados) cuando vienen de scrapers.
    """
    if not listings:
        return []
    client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))
    prompt = (
        f"Producto objetivo: {producto}\n\n"
        f"Listings candidatos (JSON):\n{json.dumps(listings, ensure_ascii=False)}\n\n"
        "Devuelve SOLO JSON array con objetos:\n"
        "[{\"tienda\":string,\"precio\":number,\"moneda\":\"EUR\",\"url\":string,\"descripcion\":string,\"disponible\":true|false}]\n\n"
        "Reglas:\n"
        "- Excluye usados/segunda mano/reacondicionados.\n"
        "- Mantén solo precios numéricos claros.\n"
        "- Ordena de menor a mayor.\n"
        "- Si ninguno sirve, devuelve []."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    text = resp.content[0].text.strip().strip("```json").strip("```").strip()
    try:
        arr = json.loads(text)
        return arr if isinstance(arr, list) else []
    except Exception:
        return []

def enrich_prices_with_scrapegraph(producto: str, raw_results: list, existing: list) -> list:
    """
    Último recurso: si no hay precios, usa ScrapeGraphAI en 1-2 URLs y filtra con Claude.
    """
    if existing:
        return existing
    if os.getenv("USE_SCRAPEGRAPH_FALLBACK", "0") != "1":
        return existing

    urls = []
    for r in raw_results[:3]:
        u = r.get("url")
        if isinstance(u, str) and u:
            urls.append(u)

    max_urls = int(os.getenv("SCRAPEGRAPH_MAX_URLS", "2"))
    urls = urls[:max_urls]

    extracted = []
    for u in urls:
        # respeta filtro de usados por texto/dominio antes de gastar scraping
        if os.getenv("EXCLUDE_USED_RESULTS", "1") == "1" and looks_used_listing(None, None, u):
            continue
        listing = scrapegraph_extract_listing(u)
        if not listing:
            continue
        # si ya dice usado/reacondicionado, descartamos
        cond = (listing.get("condicion") or "").lower()
        if os.getenv("EXCLUDE_USED_RESULTS", "1") == "1" and cond in ("usado", "reacondicionado"):
            continue
        if isinstance(listing.get("precio"), (int, float)):
            extracted.append(listing)

    return claude_filter_listings(producto, extracted)

def extract_prices(producto: str, raw_results: list) -> list:
    """
    Pipeline: primero Claude sobre snippets; si no hay precios, intenta HTML directo.
    """
    # Filtra usados antes de pasarle a Claude / HTML.
    exclude_used = os.getenv("EXCLUDE_USED_RESULTS", "1") == "1"
    filtered = raw_results
    if exclude_used and raw_results:
        filtered = [r for r in raw_results if not looks_used_listing(r.get("title"), r.get("content"), r.get("url"))]

    prices = extract_prices_with_claude(producto, filtered)
    if prices:
        return prices
    prices = enrich_prices_from_html(producto, filtered, prices)
    if prices:
        return prices
    return enrich_prices_with_scrapegraph(producto, filtered, prices)

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

# ── Scheduler per-item (según periodicidad) ───────────────────────────────────

SCHEDULER: BackgroundScheduler | None = None

def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)

def _job_id(chat_key: str, item_id: str) -> str:
    return f"item:{chat_key}:{item_id}"

def get_scheduler() -> BackgroundScheduler | None:
    return SCHEDULER

def check_item_job(chat_key: str, item_id: str):
    """
    Job: revisa un solo item y manda alertas si corresponde.
    """
    chat_watchlist = load_chat_watchlist(chat_key)
    item = chat_watchlist.get(item_id)
    if not isinstance(item, dict) or not item.get("activo", True):
        # Item ya no existe o está inactivo -> intenta remover el job
        sch = get_scheduler()
        if sch:
            try:
                sch.remove_job(_job_id(chat_key, item_id))
            except Exception:
                pass
        return

    settings = load_chat_settings(chat_key)
    location = settings.get("search_location")

    prev_precio = item.get("mejor_precio_actual")
    now_iso = datetime.datetime.now().isoformat()

    raw = search_prices(item["producto"], item.get("query_busqueda", item["producto"]), location=location)
    prices = extract_prices(item["producto"], raw)

    item["ultima_revision"] = now_iso

    periodicidad_h = item.get("periodicidad_horas")
    if periodicidad_h:
        try:
            seconds = max(60, int(float(periodicidad_h) * 3600))
            item["next_run_at"] = (utcnow() + datetime.timedelta(seconds=seconds)).isoformat()
        except Exception:
            pass

    if not prices:
        chat_watchlist[item_id] = item
        save_chat_watchlist(chat_key, chat_watchlist)
        return

    best = prices[0]
    nuevo_precio = best["precio"]

    if "historial" not in item:
        item["historial"] = []
    item["historial"].append({
        "fecha": now_iso,
        "precio": nuevo_precio,
        "tienda": best["tienda"],
        "url": best["url"]
    })
    item["mejor_precio_actual"] = nuevo_precio
    item["mejor_tienda"] = best["tienda"]
    item["mejor_url"] = best["url"]

    # Alertas
    if prev_precio is not None and nuevo_precio < prev_precio:
        bajada = prev_precio - nuevo_precio
        pct = (bajada / prev_precio) * 100 if prev_precio else 0
        msg = (
            f"🔔 Bajada de precio!\n\n"
            f"🛍️ {item['producto']}\n"
            f"💰 €{nuevo_precio:.2f} (antes €{prev_precio:.2f})\n"
            f"📉 Bajó €{bajada:.2f} ({pct:.1f}%)\n"
            f"🏪 {best['tienda']}\n"
            f"🔗 {best['url']}"
        )
        send_message(chat_key, msg)

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
    save_chat_watchlist(chat_key, chat_watchlist)

def schedule_item_job(chat_key: str, item_id: str, periodicidad_horas: float):
    sch = get_scheduler()
    if not sch:
        return

    seconds = max(60, int(float(periodicidad_horas) * 3600))
    next_run = utcnow() + datetime.timedelta(seconds=seconds)

    sch.add_job(
        check_item_job,
        "interval",
        id=_job_id(chat_key, item_id),
        seconds=seconds,
        next_run_time=next_run,
        replace_existing=True,
        args=[chat_key, item_id],
        misfire_grace_time=3600,
        max_instances=1,
        coalesce=True,
    )

    # Persistimos next_run_at en storage
    chat_watchlist = load_chat_watchlist(chat_key)
    if item_id in chat_watchlist and isinstance(chat_watchlist[item_id], dict):
        chat_watchlist[item_id]["next_run_at"] = next_run.isoformat()
        save_chat_watchlist(chat_key, chat_watchlist)

def unschedule_item_job(chat_key: str, item_id: str):
    sch = get_scheduler()
    if not sch:
        return
    try:
        sch.remove_job(_job_id(chat_key, item_id))
    except JobLookupError:
        pass
    except Exception:
        pass

def bootstrap_jobs_from_storage():
    """
    Reconstruye jobs desde Redis/archivo al arrancar (para sobrevivir redeploys).
    """
    sch = get_scheduler()
    if not sch:
        return

    data = load_watchlist()
    now = utcnow()

    for chat_key, chat_watchlist in (data or {}).items():
        if not isinstance(chat_watchlist, dict):
            continue
        for item_id, item in chat_watchlist.items():
            if not isinstance(item, dict) or not item.get("activo", True):
                continue
            periodicidad_h = item.get("periodicidad_horas")
            if not periodicidad_h:
                continue
            try:
                seconds = max(60, int(float(periodicidad_h) * 3600))
            except Exception:
                continue

            # next_run_time: si hay next_run_at futuro, úsalo; sino desde ahora.
            next_run = now + datetime.timedelta(seconds=seconds)
            try:
                nra = item.get("next_run_at")
                if isinstance(nra, str) and nra:
                    dt = datetime.datetime.fromisoformat(nra)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    if dt > now:
                        next_run = dt
            except Exception:
                pass

            sch.add_job(
                check_item_job,
                "interval",
                id=_job_id(str(chat_key), str(item_id)),
                seconds=seconds,
                next_run_time=next_run,
                replace_existing=True,
                args=[str(chat_key), str(item_id)],
                misfire_grace_time=3600,
                max_instances=1,
                coalesce=True,
            )

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

        settings = load_chat_settings(str(chat_key))
        location = settings.get("search_location")

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

            # sesgo por ubicación
            raw = search_prices(item["producto"], item.get("query_busqueda", item["producto"]), location=location)
            prices = extract_prices(item["producto"], raw)

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

def check_chat_prices_and_report(chat_key: str):
    """
    Fuerza una revisión inmediata solo para un chat y devuelve un resumen por Telegram.
    """
    chat_watchlist = load_chat_watchlist(chat_key)
    active = {k: v for k, v in chat_watchlist.items() if isinstance(v, dict) and v.get("activo", True)}
    if not active:
        send_message(chat_key, "📋 No tienes productos activos. Envía lo que quieres comprar para agregarlo.")
        return

    settings = load_chat_settings(chat_key)
    location = settings.get("search_location")

    now_iso = datetime.datetime.now().isoformat()
    lines = [f"🔎 Revisión forzada ({len(active)} productos):"]

    for i, (item_id, item) in enumerate(active.items(), 1):
        try:
            raw = search_prices(item["producto"], item.get("query_busqueda", item["producto"]), location=location)
            prices = extract_prices(item["producto"], raw)
            if prices:
                best = prices[0]
                nuevo_precio = best["precio"]
                item["mejor_precio_actual"] = nuevo_precio
                item["mejor_tienda"] = best["tienda"]
                item["mejor_url"] = best["url"]
                item["ultima_revision"] = now_iso
                if "historial" not in item:
                    item["historial"] = []
                item["historial"].append({
                    "fecha": now_iso,
                    "precio": nuevo_precio,
                    "tienda": best["tienda"],
                    "url": best["url"]
                })
                lines.append(f"{i}. ✅ {item['producto']}\n   💰 €{nuevo_precio:.2f} — {best['tienda']}\n   🔗 {best['url']}")
            else:
                item["ultima_revision"] = now_iso
                lines.append(f"{i}. ⚠️ {item['producto']}\n   Sin precio claro todavía.")

            chat_watchlist[item_id] = item
        except Exception as e:
            lines.append(f"{i}. ❌ {item.get('producto','(producto)')}\n   Error buscando: {type(e).__name__}")

    save_chat_watchlist(chat_key, chat_watchlist)
    send_message(chat_key, "\n\n".join(lines))

def check_single_async(chat_key: str, item_id: str):
    """Busca precio inicial de un producto recién agregado."""
    def _run():
        import time; time.sleep(3)
        try:
            chat_watchlist = load_chat_watchlist(chat_key)
            if item_id not in chat_watchlist:
                return
            item = chat_watchlist[item_id]
            settings = load_chat_settings(chat_key)
            location = settings.get("search_location")

            raw = search_prices(item["producto"], item.get("query_busqueda", item["producto"]), location=location)
            prices = extract_prices(item["producto"], raw)
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
            else:
                # Feedback explícito: si no hay precios, lo decimos.
                send_message(
                    chat_key,
                    f"⚠️ No encontré un precio claro todavía para:\n{item['producto']}\n\n"
                    "Lo seguiré revisando según la periodicidad."
                )
        except Exception as e:
            print(f"❌ check_single_async failed: {e}")
    threading.Thread(target=_run, daemon=True).start()

def check_batch_initial_async(chat_key: str, item_ids: list[str]):
    """
    Procesa búsquedas iniciales de un batch de forma secuencial (menos ruido y menos carga).
    """
    def _run():
        import time
        ok, miss = 0, 0
        for iid in item_ids:
            # Reusa la lógica existente (envía mensajes por item)
            check_single_async(chat_key, iid)
            # Evita disparar demasiadas búsquedas al mismo tiempo
            time.sleep(0.6)
            # No contamos realmente acá porque es async; solo damos un cierre al final
        time.sleep(2)
        send_message(chat_key, "✅ Arranqué la búsqueda inicial de tu listado. Te voy mandando resultados a medida que salen.")
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

    # Intro 1 vez por chat (no interrumpe /start)
    try:
        settings_intro = load_chat_settings(chat_key)
        if not settings_intro.get("intro_sent"):
            telegram_send(chat_id, build_intro_message(settings_intro))
            settings_intro["intro_sent"] = True
            save_chat_settings(chat_key, settings_intro)
    except Exception as e:
        print(f"❌ intro send failed: {e}")

    # Router LLM (decide intent; fallback a reglas si falla)
    routed = route_intent_with_claude(clean_text)
    if routed and isinstance(routed, dict):
        intent = routed.get("intent")
        if intent == "out_of_scope":
            telegram_send(
                chat_id,
                "Lo siento, solo puedo ayudarte con tracking de precios.\n\n"
                "Prueba con: `listar`, `forzar busqueda`, `comprado 2`, `eliminar 1`, "
                "`sincronizacion 2`, `ubicacion España`, o pegar un listado de productos."
            )
            return "", 204

        # Traduce intent a "norm" para reutilizar handlers existentes.
        if intent in ("greeting","listar","forzar_busqueda","ubicacion","sync_jobs","comprado","eliminar"):
            norm = {"kind": "command", "command": None, "clean_text": clean_text}
            if intent == "greeting":
                norm["command"] = "greeting"
            elif intent == "listar":
                norm["command"] = "listar"
            elif intent == "forzar_busqueda":
                norm["command"] = "forzar_busqueda"
            elif intent == "ubicacion":
                norm["command"] = "ubicacion"
                norm["location"] = (routed.get("location") or "").strip()
            elif intent == "sync_jobs":
                norm["command"] = "sync_jobs"
                ph = routed.get("periodicidad_horas")
                norm["sync_arg"] = (str(int(ph)) if isinstance(ph, (int, float)) and float(ph).is_integer() else (str(ph) if ph else "")).strip()
            elif intent in ("comprado","eliminar"):
                norm["command"] = intent
                norm["numbers"] = routed.get("numbers") or []

    # Si falta ubicación, preguntamos una vez y reanudamos luego
    pending_loc = load_pending_location(chat_key)
    if pending_loc is not None:
        loc = clean_text.strip()
        if not loc:
            telegram_send(chat_id, "¿En qué país/ciudad/region quieres que busque? Ej: España, Madrid, UE.")
            return "", 204
        settings = load_chat_settings(chat_key)
        settings["search_location"] = loc
        save_chat_settings(chat_key, settings)
        original = pending_loc.get("original_text")
        save_pending_location(chat_key, None)
        if isinstance(original, str) and original.strip():
            # re-procesa el mensaje original
            norm = normalize_user_message(original)
            clean_text = norm["clean_text"]
        else:
            telegram_send(chat_id, f"✅ Ok, voy a priorizar resultados en: {loc}")
            return "", 204

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

            # Agenda jobs por item según periodicidad
            for iid in created_ids:
                it = chat_watchlist.get(iid)
                if isinstance(it, dict) and it.get("periodicidad_horas"):
                    try:
                        schedule_item_job(chat_key, iid, float(it["periodicidad_horas"]))
                    except Exception:
                        pass

            telegram_send(
                chat_id,
                f"✅ Listo! Agregué {len(created_ids)} productos.\n\n"
                "Buscando precio inicial (puede tardar un poco)..."
            )
            # Dispara búsquedas iniciales (en background) de forma controlada
            check_batch_initial_async(chat_key, created_ids)
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

        # Agenda job por item (desde ahora + periodicidad)
        try:
            schedule_item_job(chat_key, item_id, float(periodicidad_h))
        except Exception:
            pass

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
        if cmd == "greeting":
            settings_greet = load_chat_settings(chat_key)
            telegram_send(chat_id, build_greeting_message(settings_greet))
            return "", 204
        if cmd == "listar":
            telegram_send(chat_id, format_watchlist(chat_watchlist))
            return "", 204
        if cmd == "forzar_busqueda":
            telegram_send(chat_id, "⏳ OK, forzando búsqueda ahora. Te mando un resumen en un momento…")
            def _run():
                try:
                    check_chat_prices_and_report(chat_key)
                except Exception as e:
                    print(f"❌ forzar_busqueda failed: {e}")
                    try:
                        telegram_send(chat_id, "⚠️ No pude completar la búsqueda forzada. Intenta de nuevo en 1 minuto.")
                    except Exception:
                        pass
            threading.Thread(target=_run, daemon=True).start()
            return "", 204
        if cmd == "sync_jobs":
            arg = (norm.get("sync_arg") or "").strip()
            horas = parse_periodicidad_horas_from_text(arg) if arg else None
            if not horas:
                telegram_send(
                    chat_id,
                    "¿Cada cuántas horas querés que revise *todos* tus productos?\n"
                    "Ejemplos: `sincronizacion 2`, `sincronizacion 12`, `sincronizacion diario`."
                )
                return "", 204

            active_ids = [iid for iid, it in chat_watchlist.items() if isinstance(it, dict) and it.get("activo", True)]
            if not active_ids:
                telegram_send(chat_id, "📋 No tenés productos activos. Agregá uno primero.")
                return "", 204

            updated = 0
            for iid in active_ids:
                it = chat_watchlist.get(iid)
                if not isinstance(it, dict):
                    continue
                it["periodicidad_horas"] = float(horas)
                # resetea el próximo run (se recalcula al schedule)
                it.pop("next_run_at", None)
                chat_watchlist[iid] = it
                try:
                    schedule_item_job(chat_key, iid, float(horas))
                except Exception:
                    pass
                updated += 1

            save_chat_watchlist(chat_key, chat_watchlist)
            settings = load_chat_settings(chat_key)
            settings["default_periodicidad_horas"] = float(horas)
            save_chat_settings(chat_key, settings)

            telegram_send(chat_id, f"✅ Listo. Actualicé {updated} productos para revisar cada {float(horas):g}h.")
            return "", 204
        if cmd in ("comprado", "eliminar"):
            nums = norm.get("numbers") or []
            active_keys = [k for k, v in chat_watchlist.items() if v.get("activo", True)]
            if not nums:
                telegram_send(chat_id, "⚠️ Dime el/los números. Ej: `comprado 2` o `comprado 2,3,4`.")
                return "", 204

            marcados = []
            fuera_de_rango = []
            for num in nums:
                if 1 <= num <= len(active_keys):
                    iid = active_keys[num - 1]
                    nombre = chat_watchlist[iid]["producto"]
                    chat_watchlist[iid]["activo"] = False
                    chat_watchlist[iid]["fecha_compra"] = datetime.date.today().isoformat()
                    marcados.append(f"{num}. {nombre}")
                    try:
                        unschedule_item_job(chat_key, iid)
                    except Exception:
                        pass
                else:
                    fuera_de_rango.append(str(num))

            save_chat_watchlist(chat_key, chat_watchlist)

            if marcados:
                msg = "🎉 Listo! Dejé de trackear:\n" + "\n".join(marcados)
                if fuera_de_rango:
                    msg += "\n\n⚠️ Fuera de rango: " + ", ".join(fuera_de_rango)
                msg += "\n\n¿Qué más quieres buscar?"
                telegram_send(chat_id, msg)
            else:
                telegram_send(chat_id, "⚠️ Los números no coinciden con tu lista. Escribe `listar` y prueba de nuevo.")
            return "", 204
        if cmd == "ayuda":
            settings_help = load_chat_settings(chat_key)
            telegram_send(chat_id, build_intro_message(settings_help))
            return "", 204
        if cmd == "ubicacion":
            loc = (norm.get("location") or "").strip()
            if not loc:
                telegram_send(chat_id, "Usa: `ubicacion España` (o `ubicacion Madrid`).")
                return "", 204
            settings = load_chat_settings(chat_key)
            settings["search_location"] = loc
            save_chat_settings(chat_key, settings)
            telegram_send(chat_id, f"✅ Ubicación guardada. Voy a priorizar resultados en: {loc}")
            return "", 204

    # 3) Batch detection (sin LLM para la intención; sí LLM para extraer items)
    if norm["kind"] == "batch":
        settings = load_chat_settings(chat_key)
        if not settings.get("search_location"):
            save_pending_location(chat_key, {"original_text": clean_text})
            telegram_send(chat_id, "¿En qué país/ciudad/region quieres que busque estos productos? Ej: España.")
            return "", 204

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
        settings = load_chat_settings(chat_key)
        if not settings.get("search_location"):
            save_pending_location(chat_key, {"original_text": clean_text})
            telegram_send(chat_id, "¿En qué país/ciudad/region quieres que busque? Ej: España, Madrid, UE.")
            return "", 204

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
        num_raw = intent.get("numero_item")
        nums = num_raw if isinstance(num_raw, list) else ([num_raw] if isinstance(num_raw, int) else None)
        active_keys = [k for k, v in chat_watchlist.items() if v.get("activo", True)]
        if nums:
            marcados = []
            fuera_de_rango = []
            for n in nums:
                if isinstance(n, int) and 1 <= n <= len(active_keys):
                    iid = active_keys[n - 1]
                    nombre = chat_watchlist[iid]["producto"]
                    chat_watchlist[iid]["activo"] = False
                    chat_watchlist[iid]["fecha_compra"] = datetime.date.today().isoformat()
                    marcados.append(f"{n}. {nombre}")
                    try:
                        unschedule_item_job(chat_key, iid)
                    except Exception:
                        pass
                else:
                    fuera_de_rango.append(str(n))
            save_chat_watchlist(chat_key, chat_watchlist)
            if marcados:
                reply = "🎉 Listo! Dejé de trackear:\n" + "\n".join(marcados)
                if fuera_de_rango:
                    reply += "\n\n⚠️ Fuera de rango: " + ", ".join(fuera_de_rango)
                reply += "\n\n¿Qué más quieres buscar?"
            else:
                reply = "⚠️ Los números no coinciden con tu lista. Escribe `listar` y prueba de nuevo."
        else:
            reply = "⚠️ Dime el/los números. Ej: `comprado 2` o `comprado 2,3,4`."

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
    # Scheduler: jobs por producto según periodicidad_horas
    SCHEDULER = BackgroundScheduler()
    SCHEDULER.start()
    bootstrap_jobs_from_storage()
    print("⏰ Scheduler activo — jobs por producto (según periodicidad)")

    port = int(os.environ.get("PORT", 5001))
    print(f"🌐 Webhook en puerto {port}")
    print("✅ Price Tracker Agent corriendo en Railway!\n")

    app.run(host="0.0.0.0", port=port, debug=False)
