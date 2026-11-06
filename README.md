# API de consultas RENIEC y SUNARP

## Configuración

1. Instala las dependencias de la API con `pip install -r requirements.txt`.
2. Define `DATABASE_URL` y `API_KEY` en el entorno. Genera una clave con:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. Para los workers distribuidos, define `WORKER_SECRET` (una clave compartida entre el VPS y tus PCs).

`GET /health` comprueba que la API responde. `GET /health/db` comprueba también
la conexión con PostgreSQL y devuelve `503` si `DATABASE_URL` no está configurada
o la base de datos no está disponible. El `HEALTHCHECK` de Docker usa este
segundo endpoint.

---

## Arquitectura Master-Worker (24/7 en la nube)

Permite que tu servidor VPS (en AWS, DigitalOcean, etc.) esté disponible 24/7 para tus clientes, mientras que las consultas a SUNARP se procesan automáticamente desde tus PCs con Windows en Perú (usando su IP residencial local).

```
[Cliente] ──POST /consulta──▶ [VPS Master] ◀──Polling── [Worker PC (Perú)]
                                   │                          │
                                   └────────Resultado─────────┘
```

### 1. En el VPS (Master)
Arranca la API normalmente:
```bash
uvicorn sunarp_api:app --host 0.0.0.0 --port 8000
```
Los endpoints disponibles son:
- `POST /consulta` (público para clientes con `X-API-Key`)
- `GET /worker/tarea` (para que los workers consulten placas pendientes)
- `POST /worker/resultado` (para que los workers entreguen los datos)
- `GET /worker/status` (ver cuántos workers y tareas hay activas)

### 2. En las PCs locales (Workers)
En cada máquina en Perú (puedes tener 1, 2 o más PCs encendidas):
1. Configura en su `.env`:
   ```env
   VPS_URL=https://tu-vps.com
   WORKER_SECRET=tu_clave_secreta_aqui
   ```
2. Ejecuta el worker haciendo doble clic en `iniciar_worker.bat` o:
   ```bash
   python worker.py
   ```

### 3. Compilar el Worker a un archivo `.exe` portable (Opcional)
Para llevar el worker a cualquier PC sin necesidad de instalar Python:
```bash
pip install pyinstaller
pyinstaller --onefile --name "WorkerSUNARP" worker.py
```
El archivo ejecutable generado quedará en la carpeta `dist/WorkerSUNARP.exe`.

---

## Consulta CLI directa (Sin VPS)

```bash
python sunarp_consulta.py 1274UP
```
