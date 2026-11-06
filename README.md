# API de consultas RENIEC y SUNARP

## Configuración

1. Instala las dependencias de la API con `pip install -r requirements.txt`.
2. Define `DATABASE_URL` y `API_KEY` en el entorno. Genera una clave con:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. La clave debe tener al menos 32 caracteres. No guardes el `.env` en Git ni
   incluyas credenciales en imágenes Docker.
4. PostgreSQL se conecta sin SSL (`sslmode=disable`) para ser compatible con
   servidores que no ofrecen TLS. Usa esta configuración solo si la base de
   datos es accesible exclusivamente por una red privada confiable.

### Bright Data Scraping Browser (opcional)

Define `BRD_SBR_ENDPOINT` con la URL `wss://` completa obtenida del panel de
Bright Data (**Puppeteer / Playwright** → cadena con credenciales incrustadas).
Cuando esta variable está presente, **tanto `sunarp_api.py` como
`sunarp_consulta.py`** usan el navegador remoto en lugar de Chromium local.
El servicio de Bright Data resuelve Cloudflare Turnstile automáticamente.

```
BRD_SBR_ENDPOINT=wss://brd-customer-hl_XXXX-zone-scraping_browser1:PASS@brd.superproxy.io:9222
```

Si la variable no está definida, el comportamiento es idéntico al anterior
(Chromium local / Chrome con CDP).

La API combinada ofrece `POST /reniec/consulta` y `POST /sunarp/consulta`.
La API SUNARP independiente ofrece `POST /consulta`; `GET /health` informa
además el modo activo (`"browser_mode": "bright_data"` o `"local"`).
Las rutas de consulta requieren el encabezado `X-API-Key`. Se permiten hasta
60 consultas autenticadas por minuto por proceso. Para despliegues con varios
procesos, aplica además límites en el gateway o proxy.

Envía el DNI / placa en el cuerpo JSON del POST, no en la URL. Publica el
servicio solo detrás de un proxy HTTPS; el puerto de la API no cifra por sí
mismo el tráfico del cliente. La conexión API-PostgreSQL tampoco cifra datos;
no expongas el puerto de PostgreSQL a Internet.

## Ejecución y pruebas

En local, instala Chromium de Playwright con `python -m playwright install chromium`
y arranca la API con `uvicorn sunarp_api:app --host 127.0.0.1 --port 8000`.
La imagen principal puede construirse desde la raíz con `docker build -t consultas .`;
proporciona las variables al contenedor en tiempo de ejecución, no durante el build.
En EasyPanel u otro panel, configura el puerto interno `8000` y usa el Dockerfile
de la raíz. Uvicorn escribe los logs de arranque y acceso en la salida del
contenedor; el healthcheck consulta `/health` cada 30 segundos.

### CLI de consulta SUNARP

```bash
# Modo Bright Data (si BRD_SBR_ENDPOINT está en .env o en el entorno)
python sunarp_consulta.py ABC123

# Forzar timeout mayor
python sunarp_consulta.py 1274UP --timeout 120
```

Ejecuta las pruebas unitarias seguras (no consultan la base de datos) con
`python -m unittest -v`.

El importador RENIEC usa las dependencias de `requirements.txt`. El addon
opcional de metadatos de tráfico se instala con
`pip install -r requirements-traffic.txt`.

El addon no registra rutas, encabezados, cuerpos ni datos personales; los
metadatos se rotan en un máximo de cuatro archivos de 5 MB.
