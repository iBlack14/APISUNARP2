#!/usr/bin/env python3
"""
API unificada: RENIEC (DNI) + SUNARP (placa vehicular).

Endpoints:
  GET  /health
  POST /reniec/consulta          {"dni": "12345678"}
  POST /sunarp/consulta          {"placa": "ABC123"}
  POST /consulta                 {"placa": "ABC123"}  (alias)

Endpoints de Workers (Master-Worker):
  GET  /worker/tarea             (el worker solicita tareas pendientes)
  POST /worker/resultado         (el worker entrega la respuesta)
  GET  /worker/status            (estado de workers conectados)

Variables de entorno:
  DATABASE_URL   (requerida para RENIEC)
  API_KEY        (clave de acceso para clientes, mínimo 32 caracteres)
  WORKER_SECRET  (clave secreta compartida entre VPS y Workers)
"""

import asyncio
import hmac
import logging
import os
import re
import subprocess
import threading
import time
import uuid

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright
from psycopg.rows import dict_row

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("api")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
API_KEY = os.environ.get("API_KEY", "").strip()
WORKER_SECRET = os.environ.get("WORKER_SECRET", "J6mK8zV9xL4wQ2pA7rT1yU5iO3eB0sD-cN8vF2gH1jE").strip()

RATE_LIMIT = 60
RATE_WINDOW_SECONDS = 60
_rate_lock = threading.Lock()
_rate_window_started = 0.0
_rate_count = 0

# Cola y estado para arquitectura Master-Worker
_task_queue = asyncio.Queue()
_pending_tasks = {}  # task_id -> {"placa": str, "future": asyncio.Future, "created_at": float}
_last_worker_seen = 0.0

PAGE_URL = "https://consultavehicular.sunarp.gob.pe/consulta-vehicular/inicio"
SUNARP_API_URL = (
    "https://api-gateway.sunarp.gob.pe:9443/sunarp/multiservicios/"
    "multiservicio-consvehicular/consulta/getDatosVehiculo"
)
IP_URL = "https://api.ipify.org/?format=json"
CDP_PORT = int(os.environ.get("SUNARP_CDP_PORT", "9222"))
CHROME_PATH = os.environ.get(
    "CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)
CHROME_PROFILE = os.environ.get("CHROME_PROFILE", r"C:\chrome-debug-profile")
SUNARP_CLIENT_ID = "70574c7d9194834316a156b1d68fdb90"

app = FastAPI(title="API Consultas", version="1.0")

SEXO_MAP = {"1": "Masculino", "2": "Femenino", "M": "Masculino", "F": "Femenino"}


# ---------------------------------------------------------------- RENIEC --

def get_conn():
    return psycopg.connect(
        DATABASE_URL, connect_timeout=10, sslmode="disable"
    )


def require_api_key(
    api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    if len(API_KEY) < 32:
        raise HTTPException(503, "Autenticación no configurada correctamente.")
    if api_key is None or not hmac.compare_digest(
        api_key.encode("utf-8"), API_KEY.encode("utf-8")
    ):
        raise HTTPException(
            401, "No autorizado.", headers={"WWW-Authenticate": "ApiKey"}
        )

    global _rate_count, _rate_window_started
    now = time.monotonic()
    with _rate_lock:
        elapsed = now - _rate_window_started
        if elapsed >= RATE_WINDOW_SECONDS:
            _rate_window_started = now
            _rate_count = 0
        if _rate_count >= RATE_LIMIT:
            retry_after = max(1, int(RATE_WINDOW_SECONDS - elapsed))
            raise HTTPException(
                429,
                "Límite de consultas excedido.",
                headers={"Retry-After": str(retry_after)},
            )
        _rate_count += 1


def require_worker_secret(
    x_worker_secret: str | None = Header(default=None, alias="X-Worker-Secret")
) -> None:
    if not x_worker_secret or not hmac.compare_digest(
        x_worker_secret.encode("utf-8"), WORKER_SECRET.encode("utf-8")
    ):
        raise HTTPException(401, "Worker no autorizado.")


def parse_ubigeo_dir(ubigeo_dir: str):
    parts = (ubigeo_dir or "").split("-")
    if len(parts) >= 3:
        return parts[0].strip(), parts[1].strip(), "-".join(parts[2:]).strip()
    return "", "", ""


def row_to_response(row: dict) -> dict:
    dep, prov, dist = parse_ubigeo_dir(row.get("UBIGEO_DIR") or "")
    sexo_raw = (row.get("SEXO") or "").strip()
    return {
        "dni": row.get("DNI"),
        "apellido_paterno": row.get("AP_PAT"),
        "apellido_materno": row.get("AP_MAT"),
        "nombres": row.get("NOMBRES"),
        "nombre_completo": " ".join(
            p for p in [row.get("NOMBRES"), row.get("AP_PAT"), row.get("AP_MAT")] if p
        ),
        "fecha_nacimiento": row.get("FECHA_NAC"),
        "sexo": SEXO_MAP.get(sexo_raw.upper(), sexo_raw),
        "estado_civil": row.get("EST_CIVIL"),
        "direccion": row.get("DIRECCION"),
        "departamento": dep,
        "provincia": prov,
        "distrito": dist,
        "ubigeo_nacimiento": row.get("UBIGEO_NAC"),
        "ubigeo_direccion": row.get("UBIGEO_DIR"),
        "fecha_inscripcion": row.get("FCH_INSCRIPCION"),
        "fecha_emision": row.get("FCH_EMISION"),
        "fecha_caducidad": row.get("FCH_CADUCIDAD"),
        "dig_ruc": row.get("DIG_RUC"),
        "madre": row.get("MADRE"),
        "padre": row.get("PADRE"),
    }


def validar_dni(dni: str) -> str:
    dni = dni.strip()
    if not re.fullmatch(r"\d{8}", dni):
        raise HTTPException(400, "DNI debe tener 8 digitos")
    return dni


class ReniecIn(BaseModel):
    dni: str


@app.post("/reniec/consulta")
def reniec_post(req: ReniecIn, _: None = Depends(require_api_key)):
    return reniec_por_dni(req.dni)


def reniec_por_dni(dni: str):
    if not DATABASE_URL:
        raise HTTPException(503, "DATABASE_URL no configurada")
    dni = validar_dni(dni)
    try:
        conn = get_conn()
    except Exception:
        log.exception("No se pudo conectar a la base de datos RENIEC")
        raise HTTPException(503, "Base de datos no disponible.")
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute('SELECT * FROM public.reniec WHERE "DNI" = %s', (dni,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "DNI no encontrado")
    return row_to_response(row)


# ---------------------------------------------------------------- SUNARP --

_lock = asyncio.Lock()


async def get_turnstile_token_cdp(page, timeout_s: int = 90) -> str:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        token = await page.evaluate(
            """() => {
                const el = document.querySelector('[name="cf-turnstile-response"]');
                return el && el.value ? el.value : null;
            }"""
        )
        if token:
            return token
        await asyncio.sleep(2)
    raise TimeoutError("Turnstile no genero token")


async def sunarp_consultar(placa: str) -> dict:
    """Fallback local: lanza Chrome real con CDP."""
    chrome = subprocess.Popen([
        CHROME_PATH,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={CHROME_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        PAGE_URL,
    ])
    try:
        await asyncio.sleep(6)
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(
                f"http://localhost:{CDP_PORT}"
            )
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            if PAGE_URL not in page.url:
                await page.goto(
                    PAGE_URL, wait_until="domcontentloaded", timeout=60000
                )

            token = await get_turnstile_token_cdp(page)
            log.info("Turnstile token OK (%d chars)", len(token))

            ip = await page.evaluate(
                f"""async () => {{
                    try {{
                        const r = await fetch('{IP_URL}');
                        const j = await r.json();
                        return j.ip || '0.0.0.0';
                    }} catch(e) {{ return '0.0.0.0'; }}
                }}"""
            )

            body = {
                "numPlaca": placa,
                "regPubId": None,
                "oficRegId": None,
                "ipAddress": ip,
                "appVersion": "1.0",
                "dG9rZW4": token,
            }
            result = await page.evaluate(
                """async ({url, body, clientId}) => {
                    const r = await fetch(url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'x-ibm-client-id': clientId,
                        },
                        body: JSON.stringify(body),
                    });
                    return {status: r.status, data: await r.json()};
                }""",
                {
                    "url": SUNARP_API_URL,
                    "body": body,
                    "clientId": SUNARP_CLIENT_ID,
                },
            )
            await browser.close()
            return result
    finally:
        chrome.terminate()


class SunarpIn(BaseModel):
    placa: str


class WorkerResultIn(BaseModel):
    task_id: str
    status: int
    data: dict
    error: str | None = None


# --- Endpoints de Workers (Master-Worker) ---

@app.get("/worker/tarea")
async def worker_get_task(_: None = Depends(require_worker_secret)):
    """El worker (ej. en tu PC) consulta si hay tareas pendientes."""
    global _last_worker_seen
    _last_worker_seen = time.time()
    try:
        task_id = await asyncio.wait_for(_task_queue.get(), timeout=15.0)
        task_info = _pending_tasks.get(task_id)
        if not task_info or task_info["future"].done():
            return {"tarea": None}
        return {"tarea": {"task_id": task_id, "placa": task_info["placa"]}}
    except asyncio.TimeoutError:
        return {"tarea": None}


@app.post("/worker/resultado")
async def worker_post_result(req: WorkerResultIn, _: None = Depends(require_worker_secret)):
    """El worker entrega los datos de la consulta."""
    global _last_worker_seen
    _last_worker_seen = time.time()
    task_info = _pending_tasks.pop(req.task_id, None)
    if not task_info:
        return {"ok": False, "msg": "Tarea no encontrada o ya finalizada"}

    future = task_info["future"]
    if not future.done():
        if req.error:
            future.set_exception(RuntimeError(req.error))
        else:
            future.set_result({"status": req.status, "data": req.data})
    return {"ok": True}


@app.get("/worker/status")
async def worker_status():
    """Estado de los workers conectados."""
    now = time.time()
    return {
        "worker_activo": (now - _last_worker_seen) < 30.0,
        "segundos_desde_ultimo_worker": round(now - _last_worker_seen, 1) if _last_worker_seen else None,
        "tareas_en_cola": _task_queue.qsize(),
        "tareas_pendientes": len(_pending_tasks),
    }


# --- Endpoint principal de consulta vehicular ---

async def procesar_consulta_sunarp(placa_raw: str) -> dict:
    placa = placa_raw.strip().upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z0-9]{4,8}", placa):
        raise HTTPException(400, "Placa invalida")

    now = time.time()
    # Si hay un worker activo en los últimos 45 segundos, delegamos al worker
    if (now - _last_worker_seen) < 45.0:
        task_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        _pending_tasks[task_id] = {"placa": placa, "future": future, "created_at": now}
        await _task_queue.put(task_id)
        log.info("Tarea %s asignada a la cola de workers para placa %s", task_id, placa)

        try:
            result = await asyncio.wait_for(future, timeout=90.0)
        except asyncio.TimeoutError:
            _pending_tasks.pop(task_id, None)
            raise HTTPException(504, "El worker no respondio en el tiempo limite (90s).")
        except Exception as exc:
            log.exception("Error reportado por el worker")
            raise HTTPException(502, f"Error en la consulta del worker: {exc}")
    else:
        # Fallback local
        async with _lock:
            try:
                result = await sunarp_consultar(placa)
            except TimeoutError as exc:
                raise HTTPException(
                    504, "Tiempo de espera agotado al consultar SUNARP."
                ) from exc
            except Exception:
                log.exception("Error en consulta SUNARP")
                raise HTTPException(502, "Error consultando SUNARP.")

    data = result.get("data", {})
    model = data.get("model") or {}
    if "imagen" in model:
        model = {k: v for k, v in model.items() if k != "imagen"}
        data = {**data, "model": model}
    return {"http": result.get("status"), "sunarp": data}


@app.post("/sunarp/consulta")
async def sunarp_post(req: SunarpIn, _: None = Depends(require_api_key)):
    return await procesar_consulta_sunarp(req.placa)


@app.post("/consulta")
async def consulta_alias(req: SunarpIn, _: None = Depends(require_api_key)):
    """Alias para compatibilidad con clientes que llaman /consulta."""
    return await procesar_consulta_sunarp(req.placa)


@app.get("/health")
def health():
    now = time.time()
    worker_online = (now - _last_worker_seen) < 30.0
    return {
        "status": "ok",
        "worker_online": worker_online,
        "tareas_en_cola": _task_queue.qsize(),
    }


@app.get("/health/db")
def health_db():
    if not DATABASE_URL:
        raise HTTPException(503, "DATABASE_URL no configurada")

    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        finally:
            conn.close()
    except psycopg.Error as exc:
        log.exception("No se pudo verificar la conexión a la base de datos")
        raise HTTPException(503, "Base de datos no disponible.") from exc

    return {"status": "ok", "database": "connected"}


@app.on_event("shutdown")
async def shutdown():
    pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
