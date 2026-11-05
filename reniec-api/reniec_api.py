#!/usr/bin/env python3
"""
API de consulta RENIEC por DNI.

GET  /health
GET  /consulta/{dni}   -> datos de la persona
POST /consulta         -> {"dni": "12345678"}

Variables de entorno:
  DATABASE_URL  (requerida)  postgresql://user:pass@host:port/db
"""

import os
import re

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL no esta configurada")

app = FastAPI(title="RENIEC Consulta por DNI", version="1.0")

SEXO_MAP = {"1": "Masculino", "2": "Femenino", "M": "Masculino", "F": "Femenino"}


def get_conn():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


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


@app.get("/consulta/{dni}")
def consulta_get(dni: str):
    return consulta_por_dni(dni)


class ConsultaIn(BaseModel):
    dni: str


@app.post("/consulta")
def consulta_post(req: ConsultaIn):
    return consulta_por_dni(req.dni)


def consulta_por_dni(dni: str):
    dni = validar_dni(dni)
    try:
        conn = get_conn()
    except Exception as e:
        raise HTTPException(503, f"Base de datos no disponible: {e}")
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                'SELECT * FROM public.reniec WHERE "DNI" = %s', (dni,)
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "DNI no encontrado")
    return row_to_response(dict(row))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
