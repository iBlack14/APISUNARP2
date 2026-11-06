# API de consultas RENIEC y SUNARP

## Configuración

1. Instala las dependencias de la API con `pip install -r requirements.txt`.
2. Define `DATABASE_URL` y `API_KEY` en el entorno. Genera una clave con:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. Para los workers distribuidos, define `WORKER_SECRET` (una clave compartida entre el VPS y tus PCs).

`GET /health` comprueba que la API responde. `GET /health/db` comprueba también
la conexión con PostgreSQL y devuelve `503` si `DATABASE_URL` no está configurada
o la base de datos no está disponible. El `HEALTHCHECK` de Docker usa `/health`
para comprobar el proceso sin reiniciarlo si PostgreSQL está temporalmente
fuera de servicio.

---

## Arquitectura Master-Worker (24/7 en la nube)

Permite que tu servidor VPS (en AWS, DigitalOcean, etc.) esté disponible 24/7 para tus clientes, mientras que las consultas a SUNARP se procesan automáticamente desde tus PCs con Windows en Perú (usando su IP residencial local).

```
[Cliente] ──POST /consulta──▶ [VPS Master] ◀──Polling── [Worker PC (Perú)]
                                   │                          │
                                   └────────Resultado─────────┘
```

### 1. En el VPS (Master)
Arranca la API Master; las consultas SUNARP se procesan solo en los Workers:
```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```
Los endpoints disponibles son:
- `POST /consulta` (público para clientes con `X-API-Key`)
- `GET /worker/tarea` (para que los workers consulten placas pendientes)
- `POST /worker/resultado` (para que los workers entreguen los datos)
- `GET /worker/status` (ver cuántos workers y tareas hay activas)

Si no hay un Worker activo, la consulta SUNARP responde `503`; el VPS no lanza
Chrome ni intenta consultar SUNARP localmente.

### 2. En las PCs locales (Workers)
En cada máquina en Perú (puedes tener 1, 2 o más PCs encendidas):
1. Configura en su `.env`:
   ```env
   VPS_URL=https://tu-vps.com
   WORKER_SECRET=tu_clave_secreta_aqui
   ```
2. Para ejecutar desde Python, instala las dependencias del worker (incluye OCR local):
   ```bash
   pip install -r requirements-worker.txt
   ```
3. Ejecuta el worker haciendo doble clic en `iniciar_worker.bat` o:
   ```bash
   python worker.py
   ```
El worker aplica OCR localmente a la imagen devuelta por SUNARP y agrega
`sunarp.ocr.campos` y `sunarp.ocr.texto` a la respuesta. La imagen no se envía
a un servicio externo de OCR.

### 3. Compilar el Worker a un archivo `.exe` portable (Opcional)
Para llevar el worker a cualquier PC sin necesidad de instalar Python:
```bash
pip install -r requirements-worker.txt pyinstaller
pyinstaller --onefile --name "WorkerSUNARP" --collect-all playwright --collect-all certifi --collect-all rapidocr_onnxruntime --collect-all onnxruntime --collect-all cv2 --collect-all PIL worker.py
```
El archivo ejecutable generado quedará en la carpeta `dist/WorkerSUNARP.exe`.

---

## Consulta CLI directa (Sin VPS)

```bash
python sunarp_consulta.py 1274UP
```
