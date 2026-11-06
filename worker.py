#!/usr/bin/env python3
"""
Worker distribuido para Consulta Vehicular SUNARP.

Este script se ejecuta en una máquina con Windows en Perú (tu PC o cualquier otra).
Se conecta hacia tu VPS (Master), espera a que lleguen consultas de placas,
las ejecuta en tu navegador Chrome local y le devuelve el resultado al VPS.

Uso:
    python worker.py
    o hacer doble clic en iniciar_worker.bat
"""

import asyncio
import json
import logging
import os
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.error

from dotenv import load_dotenv

# Cargar .env desde la carpeta del ejecutable o el directorio de trabajo
exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
load_dotenv(os.path.join(exe_dir, ".env"))
load_dotenv()

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("worker")

# ---------------------------------------------------------------------------
# SSL Context con fallback automático para evitar errores de certificados
# ---------------------------------------------------------------------------

def get_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

SSL_CONTEXT = get_ssl_context()

# ---------------------------------------------------------------------------
# Configuración del Worker
# ---------------------------------------------------------------------------

DEFAULT_VPS = "https://bot-sunarpapi.stkkft.easypanel.host"
DEFAULT_SECRET = "J6mK8zV9xL4wQ2pA7rT1yU5iO3eB0sD-cN8vF2gH1jE"

VPS_URL = os.environ.get("VPS_URL", DEFAULT_VPS).rstrip("/")
WORKER_SECRET = os.environ.get("WORKER_SECRET", DEFAULT_SECRET).strip()

PAGE_URL = "https://consultavehicular.sunarp.gob.pe/consulta-vehicular/inicio"
API_URL = (
    "https://api-gateway.sunarp.gob.pe:9443/sunarp/multiservicios/"
    "multiservicio-consvehicular/consulta/getDatosVehiculo"
)
IP_URL = "https://api.ipify.org/?format=json"

CDP_PORT = 9222
CHROME_PATH = os.environ.get(
    "CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)
CHROME_PROFILE = os.environ.get("CHROME_PROFILE", r"C:\chrome-debug-profile")


# ---------------------------------------------------------------------------
# Helpers para la consulta local con Chrome
# ---------------------------------------------------------------------------

async def get_token_cdp(page, timeout_s: int = 90) -> str:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
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
        await asyncio.sleep(2)
    raise TimeoutError("Turnstile no genero token en el tiempo limite")


async def consultar_placa(placa: str, timeout_s: int = 90) -> dict:
    """Lanza Chrome con CDP en la máquina local y ejecuta la consulta."""
    chrome = subprocess.Popen([
        CHROME_PATH,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={CHROME_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        PAGE_URL,
    ])
    try:
        await asyncio.sleep(6)
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            if PAGE_URL not in page.url:
                await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)

            log.info("Esperando token de Cloudflare Turnstile...")
            token = await get_token_cdp(page, timeout_s)
            log.info("Token obtenido (%d caracteres)", len(token))

            ip = await page.evaluate(
                f"""async () => {{
                    try {{
                        const r = await fetch('{IP_URL}');
                        const j = await r.json();
                        return j.ip || '0.0.0.0';
                    }} catch(e) {{ return '0.0.0.0'; }}
                }}"""
            )

            placa_clean = placa.strip().upper().replace(" ", "")
            body = {
                "numPlaca": placa_clean,
                "regPubId": None,
                "oficRegId": None,
                "ipAddress": ip,
                "appVersion": "1.0",
                "dG9rZW4": token,
            }

            log.info("Consultando placa %s en SUNARP...", placa_clean)
            result = await page.evaluate(
                """async ({url, body}) => {
                    const r = await fetch(url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'x-ibm-client-id': '70574c7d9194834316a156b1d68fdb90',
                        },
                        body: JSON.stringify(body),
                    });
                    return {status: r.status, data: await r.json()};
                }""",
                {"url": API_URL, "body": body},
            )
            await browser.close()
            return result
    finally:
        chrome.terminate()


# ---------------------------------------------------------------------------
# Comunicación HTTP con el VPS Master
# ---------------------------------------------------------------------------

def pedir_tarea_al_vps() -> dict | None:
    """Pregunta al VPS si hay alguna placa pendiente."""
    url = f"{VPS_URL}/worker/tarea"
    req = urllib.request.Request(
        url,
        headers={
            "X-Worker-Secret": WORKER_SECRET,
            "User-Agent": "SUNARP-Worker/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=25, context=SSL_CONTEXT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("tarea")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            log.error("ERROR 401: Clave WORKER_SECRET invalida o rechazada por el VPS.")
        else:
            log.warning("Respuesta HTTP del VPS: %s", e)
        time.sleep(5)
        return None
    except Exception as e:
        log.warning("No se pudo contactar al VPS (%s). Reintentando...", e)
        time.sleep(4)
        return None


def enviar_resultado_al_vps(task_id: str, status: int, data: dict, error: str | None = None):
    """Entrega los datos de la consulta de vuelta al VPS."""
    url = f"{VPS_URL}/worker/resultado"
    payload = json.dumps({
        "task_id": task_id,
        "status": status,
        "data": data,
        "error": error,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Worker-Secret": WORKER_SECRET,
            "User-Agent": "SUNARP-Worker/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
            log.info("Resultado de tarea %s entregado al VPS con exito.", task_id)
    except Exception as e:
        log.error("Error al enviar resultado al VPS: %s", e)


# ---------------------------------------------------------------------------
# Bucle principal de ejecución del Worker
# ---------------------------------------------------------------------------

async def main_loop():
    print("=" * 65)
    print("  INICIANDO WORKER DISTRIBUIDO SUNARP")
    print(f"  VPS Destino: {VPS_URL}")
    print(f"  Chrome:      {CHROME_PATH}")
    print("  Esperando tareas del VPS en segundo plano...")
    print("=" * 65)

    while True:
        try:
            tarea = pedir_tarea_al_vps()
            if not tarea:
                await asyncio.sleep(1)
                continue

            task_id = tarea["task_id"]
            placa = tarea["placa"]
            log.info("[+] TAREA RECIBIDA -> Placa: %s (Task ID: %s)", placa, task_id)

            try:
                result = await consultar_placa(placa, timeout_s=90)
                status = result.get("status", 200)
                data = result.get("data", {})
                enviar_resultado_al_vps(task_id, status=status, data=data)
                log.info("[✓] Tarea completada para placa %s", placa)
            except Exception as e:
                log.exception("Error al procesar consulta para placa %s: %s", placa, e)
                enviar_resultado_al_vps(task_id, status=500, data={}, error=str(e))

        except KeyboardInterrupt:
            print("\nWorker detenido por el usuario.")
            break
        except Exception as e:
            log.exception("Error en bucle del worker: %s", e)
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main_loop())

