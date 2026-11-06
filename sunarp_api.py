#!/usr/bin/env python3
"""
API REST para consulta vehicular SUNARP.

POST /consulta  {"placa": "1274UP"}  -> resultado JSON
GET  /health                              -> {"status": "ok"}

Modos de navegador:
  - LOCAL (por defecto): lanza Chromium headless en el servidor.
  - BRIGHT DATA (si BRD_SBR_ENDPOINT esta definido en el entorno):
      conecta al Scraping Browser de Bright Data via CDP/WebSocket.
      El navegador remoto resuelve Cloudflare Turnstile automaticamente.

Cada consulta usa un contexto/pagina nuevo. Una consulta a la vez (lock)
para no saturar el credito de Bright Data ni el Chromium local.
"""

import asyncio
import hmac
import logging
import os
import re
import threading
import time
import uuid

from dotenv import load_dotenv
load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sunarp-api")

PAGE_URL = "https://consultavehicular.sunarp.gob.pe/consulta-vehicular/inicio"
API_URL = (
    "https://api-gateway.sunarp.gob.pe:9443/sunarp/multiservicios/"
    "multiservicio-consvehicular/consulta/getDatosVehiculo"
)
IP_URL = "https://api.ipify.org/?format=json"

def format_sbr_endpoint(endpoint: str) -> str:
    ep = (endpoint or "").strip()
    if not ep:
        return ""
    if ep.startswith("wss://") or ep.startswith("ws://"):
        return ep
    parts = ep.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"wss://{user}:{password}@{host}:{port}"
    return f"wss://{ep}"

# Bright Data Scraping Browser endpoint (wss://user:pass@brd.superproxy.io:9222)
BRD_SBR_ENDPOINT = format_sbr_endpoint(os.environ.get("BRD_SBR_ENDPOINT", ""))
USE_BRIGHT_DATA = bool(BRD_SBR_ENDPOINT)

app = FastAPI(title="SUNARP Consulta Vehicular", version="1.0")
API_KEY = os.environ.get("API_KEY", "").strip()
WORKER_SECRET = os.environ.get("WORKER_SECRET", "mi_super_secreto_worker_123").strip()

RATE_LIMIT = 60
RATE_WINDOW_SECONDS = 60
_rate_lock = threading.Lock()
_rate_window_started = 0.0
_rate_count = 0
_lock = asyncio.Lock()

# Cola y tareas para arquitectura Master-Worker
_task_queue = asyncio.Queue()
_pending_tasks = {}  # task_id -> {"placa": str, "future": asyncio.Future, "created_at": float}
_last_worker_seen = 0.0

# Estado del browser local
_browser = None
_pw = None


def require_api_key(
    api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    if len(API_KEY) < 32:
        raise HTTPException(503, "Autenticacion no configurada correctamente.")
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
                "Limite de consultas excedido.",
                headers={"Retry-After": str(retry_after)},
            )
        _rate_count += 1


# ---------------------------------------------------------------------------
# Helpers de browser
# ---------------------------------------------------------------------------

async def _get_local_browser():
    """Devuelve (o crea) el browser Chromium local persistente."""
    global _browser, _pw
    if _browser is None or not _browser.is_connected():
        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1366,768",
            ],
        )
    return _browser


async def get_turnstile_token(page, timeout_s: int = 90) -> str:
    """
    Espera el token de Cloudflare Turnstile.

    En modo Bright Data el token suele aparecer solo (el servicio lo resuelve
    automaticamente). En modo local se intenta un click en el widget como
    ayuda adicional.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    clicked = False
    while loop.time() < deadline:
        token = await page.evaluate(
            """() => {
                const el = document.querySelector('[name="cf-turnstile-response"]');
                if (el && el.value) return el.value;
                const inputs = document.querySelectorAll('input[type="hidden"], textarea');
                for (const i of inputs) {
                    if (i.value && i.value.length > 100) return i.value;
                }
                return null;
            }"""
        )
        if token:
            return token
        # En modo local intentar click en el checkbox del widget (una sola vez)
        if not USE_BRIGHT_DATA and not clicked and loop.time() > deadline - timeout_s + 8:
            clicked = True
            try:
                frame = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
                await frame.locator('input[type="checkbox"], .cb-lb, body').first.click(timeout=3000)
                log.info("Click en widget Turnstile ejecutado")
            except Exception as e:
                log.info("No se pudo clickear Turnstile: %s", e)
        await asyncio.sleep(1)
    raise TimeoutError("Turnstile no genero token")


# ---------------------------------------------------------------------------
# Consulta principal — soporta ambos modos
# ---------------------------------------------------------------------------

async def consultar(placa: str) -> dict:
    """
    Abre una pagina en el navegador (local o Bright Data), espera el token
    Turnstile y llama a la API de SUNARP.
    """
    async with async_playwright() as p:
        if USE_BRIGHT_DATA:
            log.info("Usando navegador remoto (ZenRows) para placa %s", placa)
            browser = await p.chromium.connect_over_cdp(BRD_SBR_ENDPOINT)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
            is_remote = True
        else:
            log.info("Usando Chromium local para placa %s", placa)
            browser = await _get_local_browser()
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
                locale="es-PE",
            )
            page = await context.new_page()
            is_remote = False

        try:
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=120000)
            token = await get_turnstile_token(page)
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
                """async ({url, body}) => {
                    const r = await fetch(url, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body),
                    });
                    return {status: r.status, data: await r.json()};
                }""",
                {"url": API_URL, "body": body},
            )
            return result
        finally:
            if is_remote:
                await browser.close()
            else:
                await context.close()


# ---------------------------------------------------------------------------
# Endpoints FastAPI
# ---------------------------------------------------------------------------

def require_worker_secret(
    x_worker_secret: str | None = Header(default=None, alias="X-Worker-Secret")
) -> None:
    if not x_worker_secret or not hmac.compare_digest(
        x_worker_secret.encode("utf-8"), WORKER_SECRET.encode("utf-8")
    ):
        raise HTTPException(401, "Worker no autorizado.")


class ConsultaIn(BaseModel):
    placa: str


class WorkerResultIn(BaseModel):
    task_id: str
    status: int
    data: dict
    error: str | None = None


@app.get("/health")
async def health():
    now = time.time()
    worker_online = (now - _last_worker_seen) < 30.0
    modo = "worker_distribuido" if worker_online else ("bright_data" if USE_BRIGHT_DATA else "local")
    return {
        "status": "ok",
        "browser_mode": modo,
        "worker_online": worker_online,
        "tareas_en_cola": _task_queue.qsize(),
    }


# --- Endpoints para la arquitectura Worker Distribuido ---

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


# --- Endpoint principal de consulta ---

@app.post("/consulta")
async def consulta(req: ConsultaIn, _: None = Depends(require_api_key)):
    placa = req.placa.strip().upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z0-9]{4,8}", placa):
        raise HTTPException(400, "Placa invalida")

    now = time.time()
    # Si hay un worker activo (visto en los ultimos 45 segundos), delegamos la tarea a la PC local
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
        # Fallback local o remoto
        async with _lock:
            try:
                result = await consultar(placa)
            except TimeoutError as exc:
                raise HTTPException(
                    504, "Tiempo de espera agotado al consultar SUNARP."
                ) from exc
            except Exception:
                log.exception("Error en consulta")
                raise HTTPException(502, "Error consultando SUNARP.")

    data = result.get("data", {})
    model = data.get("model") or {}
    # Quitar la imagen base64 del response para no saturar
    if "imagen" in model:
        model = {k: v for k, v in model.items() if k != "imagen"}
        data = {**data, "model": model}
    return {"http": result.get("status"), "sunarp": data}


@app.on_event("shutdown")
async def shutdown():
    global _browser, _pw
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
