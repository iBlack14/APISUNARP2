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

La API combinada ofrece `POST /reniec/consulta` y `POST /sunarp/consulta`.
La API RENIEC independiente ofrece `POST /consulta`; el servicio SUNARP
independiente también ofrece `POST /consulta`. Las rutas de consulta requieren
el encabezado `X-API-Key`. `GET /health` es público y no revela el estado de la
base de datos. Se permiten hasta 60 consultas autenticadas por minuto por
proceso. Para despliegues con varios procesos, aplica además límites en el
gateway o proxy.

Envía el DNI en el cuerpo JSON del POST, no en la URL. Publica el servicio solo
detrás de un proxy HTTPS; el puerto de la API no cifra por sí mismo el tráfico
del cliente. La conexión API-PostgreSQL tampoco cifra datos; no expongas el
puerto de PostgreSQL a Internet.

## Ejecución y pruebas

En local, instala Chromium de Playwright con `python -m playwright install chromium`
y arranca la API con `uvicorn api:app --host 127.0.0.1 --port 8000`.
La imagen principal puede construirse desde la raíz con `docker build -t consultas .`;
proporciona las variables al contenedor en tiempo de ejecución, no durante el build.
En EasyPanel u otro panel, configura el puerto interno `8000` y usa el Dockerfile
de la raíz. Uvicorn escribe los logs de arranque y acceso en la salida del
contenedor; el healthcheck consulta `/health` cada 30 segundos. Chromium se
ejecuta en modo headless, sin requerir Xvfb.

Ejecuta las pruebas unitarias seguras (no consultan la base de datos) con
`python -m unittest -v`.

El importador RENIEC usa las dependencias de `requirements.txt`. El addon
opcional de metadatos de tráfico se instala con
`pip install -r requirements-traffic.txt`.

El addon no registra rutas, encabezados, cuerpos ni datos personales; los
metadatos se rotan en un máximo de cuatro archivos de 5 MB.
