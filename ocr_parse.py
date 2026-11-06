import os
import re

# Tesseract: usar TESSERACT_CMD si existe; en Windows ruta por defecto; en Linux se busca en PATH
_tess_cmd = os.environ.get("TESSERACT_CMD", "").strip()
if _tess_cmd:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = _tess_cmd
elif os.name == "nt":
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# tessdata local del proyecto (solo si existe; en Docker se usa el del sistema)
_tessdata = os.path.abspath("tessdata")
if os.path.isdir(_tessdata) and os.path.exists(os.path.join(_tessdata, "spa.traineddata")):
    os.environ.setdefault("TESSDATA_PREFIX", _tessdata + os.sep)

import pytesseract
from PIL import Image

# Etiquetas conocidas -> campo JSON (regex tolerante a errores OCR)
LABELS = [
    (r"PLACA\s*VIGENTE", "placa_vigente"),
    (r"PLACA\s*ANTERIOR", "placa_anterior"),
    (r"N.?\s*PLACA|PLACA\s*:", "placa"),
    (r"SERIE|VIN", "serie_vin"),
    (r"MOTOR", "motor"),
    (r"COLOR", "color"),
    (r"MARCA", "marca"),
    (r"MODELO", "modelo"),
    (r"ESTADO", "estado"),
    (r"ANOTACIONES", "anotaciones"),
    (r"SEDE", "sede"),
    (r"PROPIETARIO", "propietario"),
    (r"A.O\s*DE\s*MODELO|A.O\s+MODELO", "anio_modelo"),
]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_tive_image(img) -> dict:
    """img: objeto PIL.Image o path a archivo."""
    if isinstance(img, (str, bytes)) and not hasattr(img, "size"):
        img = Image.open(img)
    data = pytesseract.image_to_data(
        img, lang="spa", output_type=pytesseract.Output.DICT
    )

    # Agrupar palabras en lineas por coordenada 'top' (tolerancia 12px)
    words = []
    for i, t in enumerate(data["text"]):
        t = t.strip()
        if not t:
            continue
        words.append({
            "text": t,
            "left": data["left"][i],
            "top": data["top"][i],
            "w": data["width"][i],
            "h": data["height"][i],
        })

    words.sort(key=lambda w: (w["top"], w["left"]))
    lines = []
    for w in words:
        placed = False
        for line in lines:
            if abs(line["top"] - w["top"]) <= 12:
                line["words"].append(w)
                line["top"] = min(line["top"], w["top"])
                placed = True
                break
        if not placed:
            lines.append({"top": w["top"], "words": [w]})

    for line in lines:
        line["words"].sort(key=lambda w: w["left"])
        line["text"] = norm(" ".join(w["text"] for w in line["words"]))

    result = {}
    used_lines = set()

    # Estrategia 1: etiqueta y valor en la misma linea (valor a la derecha)
    for idx, line in enumerate(lines):
        text = line["text"].upper()
        for pattern, field in LABELS:
            if field in result:
                continue
            m = re.search(pattern, text)
            if not m:
                continue
            # valor = texto despues del match en la misma linea
            after = text[m.end():].strip(" :.-")
            # limpiar restos de etiqueta como (S) y validar que sea un valor real
            after = re.sub(r"^\(S\)\s*", "", after).strip(" :.-")
            if after and len(after) > 2 and re.search(r"[A-Z0-9]", after):
                result[field] = after
                used_lines.add(idx)
                break

    # Estrategia 2: si no hay valor en la misma linea, buscar en lineas siguientes cercanas
    for idx, line in enumerate(lines):
        text = line["text"].upper()
        for pattern, field in LABELS:
            if field in result or idx in used_lines:
                continue
            if re.search(pattern, text):
                # buscar siguiente linea que no sea etiqueta
                for j in range(idx + 1, min(idx + 4, len(lines))):
                    cand = lines[j]["text"]
                    cand_u = cand.upper()
                    if any(re.search(p, cand_u) for p, _ in LABELS):
                        continue
                    if len(cand) > 2 and re.search(r"[A-Z0-9]", cand):
                        result[field] = cand
                        used_lines.add(j)
                        break
                break

    return result
def parse_tive_bytes(img_bytes: bytes) -> dict:
    from io import BytesIO
    return parse_tive_image(Image.open(BytesIO(img_bytes)))


def parse_tive_ocr(img_path: str) -> dict:
    return parse_tive_image(img_path)




if __name__ == "__main__":
    import json
    res = parse_tive_ocr("sample_tive.png")
    print(json.dumps(res, ensure_ascii=False, indent=2))
