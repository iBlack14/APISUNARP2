#!/usr/bin/env python3
"""
Consulta vehicular SUNARP automatizada.

Modos de navegador (se seleccionan automaticamente segun el entorno):
  - BRIGHT DATA: si BRD_SBR_ENDPOINT esta definido en el entorno o en .env,
      conecta al Scraping Browser de Bright Data via CDP/WebSocket.
      Resuelve Cloudflare Turnstile automaticamente en la nube.
  - LOCAL (por defecto): lanza Chrome REAL con remote debugging (CDP) y
      conecta Playwright. Requiere Chrome instalado en CHROME_PATH.

Uso:
    python sunarp_consulta.py <placa> [--timeout 90]
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys

from dotenv import load_dotenv  # python-dotenv (opcional pero comodo)

try:
    load_dotenv()  # carga .env si existe
except ImportError:
    pass  # python-dotenv no instalado; usar variables de entorno del sistema

from playwright.async_api import async_playwright

PAGE_URL = "https://consultavehicular.sunarp.gob.pe/consulta-vehicular/inicio"
API_URL = (
    "https://api-gateway.sunarp.gob.pe:9443/sunarp/multiservicios/"
    "multiservicio-consvehicular/consulta/getDatosVehiculo"
)
IP_URL = "https://api.ipify.org/?format=json"

# Bright Data Scraping Browser
BRD_SBR_ENDPOINT = os.environ.get("BRD_SBR_ENDPOINT", "").strip()
USE_BRIGHT_DATA = bool(BRD_SBR_ENDPOINT)

# Configuracion Chrome local (solo para modo LOCAL)
CDP_PORT = 9222
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CHROME_PROFILE = r"C:\chrome-debug-profile"


# ---------------------------------------------------------------------------
# Helpers Turnstile
# ---------------------------------------------------------------------------

async def get_token_cdp(page, timeout_s: int) -> str:
    """Espera el token Turnstile en una pagina ya abierta."""
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


# ---------------------------------------------------------------------------
# Consulta via Bright Data Scraping Browser
# ---------------------------------------------------------------------------

async def consultar_bright_data(placa: str, timeout_s: int) -> dict:
    """Consulta usando el Scraping Browser de Bright Data (sin Chrome local)."""
    print(f"[*] Conectando a Bright Data Scraping Browser...")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(BRD_SBR_ENDPOINT)
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
            print("[*] Esperando resolucion automatica de Cloudflare Turnstile (Bright Data)...")
            token = await get_token_cdp(page, timeout_s)
            print(f"[+] Token obtenido ({len(token)} chars)")

            ip = await page.evaluate(
                f"""async () => {{
                    try {{
                        const r = await fetch('{IP_URL}');
                        const j = await r.json();
                        return j.ip || '0.0.0.0';
                    }} catch(e) {{ return '0.0.0.0'; }}
                }}"""
            )
            print(f"[*] IP de salida (Bright Data): {ip}")

            placa_clean = placa.strip().upper().replace(" ", "")
            body = {
                "numPlaca": placa_clean,
                "regPubId": None,
                "oficRegId": None,
                "ipAddress": ip,
                "appVersion": "1.0",
                "dG9rZW4": token,
            }

            print(f"[*] Consultando placa {placa_clean}...")
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
            return result
        finally:
            await context.close()
            await browser.close()


# ---------------------------------------------------------------------------
# Consulta via Chrome local (CDP)
# ---------------------------------------------------------------------------

async def consultar_local(placa: str, timeout_s: int) -> dict:
    """Consulta usando Chrome local con remote debugging."""
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

            print("[*] Esperando token de Cloudflare Turnstile...")
            token = await get_token_cdp(page, timeout_s)
            print(f"[+] Token obtenido ({len(token)} chars)")

            ip = await page.evaluate(
                f"""async () => {{
                    try {{
                        const r = await fetch('{IP_URL}');
                        const j = await r.json();
                        return j.ip || '0.0.0.0';
                    }} catch(e) {{ return '0.0.0.0'; }}
                }}"""
            )
            print(f"[*] IP detectada: {ip}")

            placa_clean = placa.strip().upper().replace(" ", "")
            body = {
                "numPlaca": placa_clean,
                "regPubId": None,
                "oficRegId": None,
                "ipAddress": ip,
                "appVersion": "1.0",
                "dG9rZW4": token,
            }

            print(f"[*] Consultando placa {placa_clean}...")
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
# Selector de modo
# ---------------------------------------------------------------------------

async def consultar_placa(placa: str, timeout_s: int) -> dict:
    if USE_BRIGHT_DATA:
        return await consultar_bright_data(placa, timeout_s)
    return await consultar_local(placa, timeout_s)


# ---------------------------------------------------------------------------
# Entrypoint CLI
# ---------------------------------------------------------------------------

async def main():
    p = argparse.ArgumentParser()
    p.add_argument("placa", help="Placa vehicular, ej: ABC123 o 1274UP")
    p.add_argument("--timeout", type=int, default=90, help="Timeout Turnstile (s)")
    args = p.parse_args()

    modo = "Bright Data Scraping Browser" if USE_BRIGHT_DATA else "Chrome local"
    print(f"[*] Modo: {modo}")

    try:
        result = await consultar_placa(args.placa, args.timeout)
    except TimeoutError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    data = result.get("data", {})
    print(f"\nHTTP {result.get('status')} | cod={data.get('cod')} | {data.get('mensaje')}")
    model = data.get("model") or {}
    if model:
        resumen = {k: v for k, v in model.items() if k != "imagen"}
        print(json.dumps(resumen, ensure_ascii=False, indent=2))
        if "imagen" in model:
            print(f"[+] imagen: {len(model['imagen'])} chars base64")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])


if __name__ == "__main__":
    asyncio.run(main())
