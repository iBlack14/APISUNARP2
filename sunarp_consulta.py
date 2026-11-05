#!/usr/bin/env python3
"""
Consulta vehicular SUNARP automatizada con Playwright.

Flujo:
  1. Abre la pagina real en Chromium
  2. Espera a que Cloudflare Turnstile genere el token
  3. Extrae el token y la IP publica
  4. Llama getDatosVehiculo via fetch dentro de la pagina (mismo Origin)
  5. Devuelve el JSON de respuesta

Uso:
    python sunarp_consulta.py <placa> [--headless] [--timeout 60]
"""

import argparse
import asyncio
import json
import sys

from playwright.async_api import async_playwright

PAGE_URL = "https://consultavehicular.sunarp.gob.pe/consulta-vehicular/inicio"
API_URL = (
    "https://api-gateway.sunarp.gob.pe:9443/sunarp/multiservicios/"
    "multiservicio-consvehicular/consulta/getDatosVehiculo"
)
IP_URL = "https://api.ipify.org/?format=json"


async def get_turnstile_token(page, timeout_s: int) -> str:
    """Espera a que Turnstile llene el input cf-turnstile-response."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        token = await page.evaluate(
            """() => {
                const el = document.querySelector(
                    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
                ) || document.querySelector('[name="cf-turnstile-response"]');
                if (el && el.value) return el.value;
                // fallback: buscar en window.turnstile o inputs ocultos
                const inputs = document.querySelectorAll('input[type="hidden"]');
                for (const i of inputs) {
                    if (i.value && i.value.length > 100) return i.value;
                }
                return null;
            }"""
        )
        if token:
            return token
        await asyncio.sleep(1)
    raise TimeoutError("Turnstile no genero token en el tiempo limite")


async def consultar_placa(placa: str, headless: bool, timeout_s: int) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
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

        print(f"[*] Abriendo {PAGE_URL}")
        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)

        print("[*] Esperando token de Cloudflare Turnstile...")
        token = await get_turnstile_token(page, timeout_s)
        print(f"[+] Token obtenido ({len(token)} chars)")

        # Obtener IP publica igual que la app
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
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body),
                });
                return {status: r.status, data: await r.json()};
            }""",
            {"url": API_URL, "body": body},
        )

        await browser.close()
        return result


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("placa", help="Placa vehicular, ej: ABC123 o 1274UP")
    p.add_argument("--headless", action="store_true", help="Ejecutar sin ventana")
    p.add_argument("--timeout", type=int, default=60, help="Timeout Turnstile (s)")
    args = p.parse_args()

    try:
        result = await consultar_placa(args.placa, args.headless, args.timeout)
    except TimeoutError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    data = result.get("data", {})
    # Resumen legible
    print(f"\nHTTP {result.get('status')} | cod={data.get('cod')} | {data.get('mensaje')}")
    model = data.get("model") or {}
    if model:
        # Mostrar campos excepto la imagen gigante
        resumen = {k: v for k, v in model.items() if k != "imagen"}
        print(json.dumps(resumen, ensure_ascii=False, indent=2))
        if "imagen" in model:
            print(f"[+] imagen: {len(model['imagen'])} chars base64")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])


if __name__ == "__main__":
    asyncio.run(main())
