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

# Bright Data Scraping Browser endpoint (wss://user:pass@brd.superproxy.io:9222)
# Si esta vacio, se usa Chromium local.
BRD_SBR_ENDPOINT = os.environ.get("BRD_SBR_ENDPOINT", "").strip()
USE_BRIGHT_DATA = bool(BRD_SBR_ENDPOINT)

app = FastAPI(title="SUNARP Consulta Vehicular", version="1.0")
API_KEY = os.environ.get("API_KEY", "").strip()
RATE_LIMIT = 60
RATE_WINDOW_SECONDS = 60
_rate_lock = threading.Lock()
_rate_window_started = 0.0
_rate_count = 0
_lock = asyncio.Lock()

# Estado del browser local (no aplica en modo Bright Data, que es stateless)
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
            log.info("Usando Bright Data Scraping Browser para placa %s", placa)
            browser = await p.chromium.connect_over_cdp(BRD_SBR_ENDPOINT)
            # Bright Data crea el contexto por defecto; usamos uno nuevo limpio
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
                locale="es-PE",
            )
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
        try:
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
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
            await context.close()
            if USE_BRIGHT_DATA:
                # En Bright Data cada sesion CDP es independiente; cerramos
                await browser.close()


# ---------------------------------------------------------------------------
# Endpoints FastAPI
# ---------------------------------------------------------------------------

class ConsultaIn(BaseModel):
    placa: str


@app.get("/health")
async def health():
    modo = "bright_data" if USE_BRIGHT_DATA else "local"
    return {"status": "ok", "browser_mode": modo}


@app.post("/consulta")
async def consulta(req: ConsultaIn, _: None = Depends(require_api_key)):
    placa = req.placa.strip().upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z0-9]{4,8}", placa):
        raise HTTPException(400, "Placa invalida")

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
