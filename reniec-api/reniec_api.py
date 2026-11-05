#!/usr/bin/env python3
"""
API de consulta RENIEC por DNI.

GET  /health
POST /consulta         -> {"dni": "12345678"}

Variables de entorno:
  DATABASE_URL  (requerida)  postgresql://user:pass@host:port/db
"""

import hmac
import logging
import os
import re
import threading
import time

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
API_KEY = os.environ.get("API_KEY", "").strip()
RATE_LIMIT = 60
RATE_WINDOW_SECONDS = 60
_rate_lock = threading.Lock()
_rate_window_started = 0.0
_rate_count = 0
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL no esta configurada")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reniec-api")
app = FastAPI(title="RENIEC Consulta por DNI", version="1.0")

SEXO_MAP = {"1": "Masculino", "2": "Femenino", "M": "Masculino", "F": "Femenino"}


def get_conn():
    return psycopg.connect(
        DATABASE_URL, connect_timeout=10, sslmode="disable"
    )


def require_api_key(
    api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    if len(API_KEY) < 32:
        raise HTTPException(503, "Autenticación no configurada correctamente.")
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
                "Límite de consultas excedido.",
                headers={"Retry-After": str(retry_after)},
            )
        _rate_count += 1


def parse_ubigeo_dir(ubigeo_dir: str):
    """UBIGEO_DIR viene como DEPARTAMENTO-PROVINCIA-DISTRITO."""
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


@app.get("/health")
def health():
    return {"status": "ok"}


class ConsultaIn(BaseModel):
    dni: str


@app.post("/consulta")
def consulta_post(req: ConsultaIn, _: None = Depends(require_api_key)):
    return consulta_por_dni(req.dni)


def consulta_por_dni(dni: str):
    dni = validar_dni(dni)
    try:
        conn = get_conn()
    except Exception:
        log.exception("No se pudo conectar a la base de datos RENIEC")
        raise HTTPException(503, "Base de datos no disponible.")
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                'SELECT * FROM public.reniec WHERE "DNI" = %s', (dni,)
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "DNI no encontrado")
    return row_to_response(row)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
