#!/usr/bin/env python3
"""
API REST para consulta vehicular SUNARP.

POST /consulta  {"placa": "1274UP"}  -> resultado JSON
GET  /health                              -> {"status": "ok"}

Cada consulta abre una sesion Playwright, resuelve Turnstile y llama
getDatosVehiculo. Una consulta a la vez (lock) para no saturar.
"""

import asyncio
import json
import logging
import re
import sys

from fastapi import FastAPI, HTTPException
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

app = FastAPI(title="SUNARP Consulta Vehicular", version="1.0")
_lock = asyncio.Lock()
_browser = None
_pw = None


async def get_browser():
    global _browser, _pw
    if _browser is None or not _browser.is_connected():
        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
    return _browser


async def get_turnstile_token(page, timeout_s: int = 90) -> str:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
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
        await asyncio.sleep(1)
    raise TimeoutError("Turnstile no genero token")


async def consultar(placa: str) -> dict:
    browser = await get_browser()
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


class ConsultaIn(BaseModel):
    placa: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/consulta")
async def consulta(req: ConsultaIn):
    placa = req.placa.strip().upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z0-9]{4,8}", placa):
        raise HTTPException(400, "Placa invalida")

    async with _lock:
        try:
            result = await consultar(placa)
        except TimeoutError as e:
            raise HTTPException(504, str(e))
        except Exception as e:
            log.exception("Error en consulta")
            raise HTTPException(502, f"Error consultando SUNARP: {e}")

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
