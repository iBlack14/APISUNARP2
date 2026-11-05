#!/usr/bin/env python3
"""
API unificada: RENIEC (DNI) + SUNARP (placa vehicular).

Endpoints:
  GET  /health
  GET  /reniec/consulta/{dni}
  POST /reniec/consulta          {"dni": "12345678"}
  POST /sunarp/consulta          {"placa": "ABC123"}

Variables de entorno:
  DATABASE_URL  (requerida para RENIEC)
"""

import asyncio
import logging
import os
import re

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("api")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

PAGE_URL = "https://consultavehicular.sunarp.gob.pe/consulta-vehicular/inicio"
SUNARP_API_URL = (
    "https://api-gateway.sunarp.gob.pe:9443/sunarp/multiservicios/"
    "multiservicio-consvehicular/consulta/getDatosVehiculo"
)
IP_URL = "https://api.ipify.org/?format=json"

app = FastAPI(title="API Consultas", version="1.0")

SEXO_MAP = {"1": "Masculino", "2": "Femenino", "M": "Masculino", "F": "Femenino"}

# ---------------------------------------------------------------- RENIEC --

def get_conn():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


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


@app.get("/reniec/consulta/{dni}")
def reniec_get(dni: str):
    return reniec_por_dni(dni)


class ReniecIn(BaseModel):
    dni: str


@app.post("/reniec/consulta")
def reniec_post(req: ReniecIn):
    return reniec_por_dni(req.dni)


def reniec_por_dni(dni: str):
    if not DATABASE_URL:
        raise HTTPException(503, "DATABASE_URL no configurada")
    dni = validar_dni(dni)
    try:
        conn = get_conn()
    except Exception as e:
        raise HTTPException(503, f"Base de datos no disponible: {e}")
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM public.reniec WHERE "DNI" = %s', (dni,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "DNI no encontrado")
    return row_to_response(dict(row))


# ---------------------------------------------------------------- SUNARP --

_lock = asyncio.Lock()
_browser = None
_pw = None


async def get_browser():
    global _browser, _pw
    if _browser is None or not _browser.is_connected():
        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=False,
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
        if not clicked and loop.time() > deadline - timeout_s + 8:
            clicked = True
            try:
                frame = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
                await frame.locator('input[type="checkbox"], .cb-lb, body').first.click(timeout=3000)
                log.info("Click en widget Turnstile ejecutado")
            except Exception as e:
                log.info("No se pudo clickear Turnstile: %s", e)
        await asyncio.sleep(1)
    raise TimeoutError("Turnstile no genero token")


async def sunarp_consultar(placa: str) -> dict:
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
            {"url": SUNARP_API_URL, "body": body},
        )
        return result
    finally:
        await context.close()


class SunarpIn(BaseModel):
    placa: str


@app.post("/sunarp/consulta")
async def sunarp_post(req: SunarpIn):
    placa = req.placa.strip().upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z0-9]{4,8}", placa):
        raise HTTPException(400, "Placa invalida")

    async with _lock:
        try:
            result = await sunarp_consultar(placa)
        except TimeoutError as e:
            raise HTTPException(504, str(e))
        except Exception as e:
            log.exception("Error en consulta SUNARP")
            raise HTTPException(502, f"Error consultando SUNARP: {e}")

    data = result.get("data", {})
    model = data.get("model") or {}
    if "imagen" in model:
        model = {k: v for k, v in model.items() if k != "imagen"}
        data = {**data, "model": model}
    return {"http": result.get("status"), "sunarp": data}


@app.get("/health")
def health():
    return {"status": "ok", "reniec": bool(DATABASE_URL)}


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
