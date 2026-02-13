#!/usr/bin/env python3
import pathlib
import sys
import re
import json
import time
import requests
from textwrap import dedent
from pypdf import PdfReader

# ---------------------------------------------------------
# CONFIG
# Modelos:
#   - "llama3:latest"
#   - "qwen2.5-coder:7b"
#   - "deepseek-r1:latest"
#   - "mistral:instruct"
# ---------------------------------------------------------
ARCHIVO_PDF  = "Apuntes_T4_y_T5.pdf"
ARCHIVO_MD   = "Apuntes_T4_y_T5.md"
ARCHIVO_TEST = "Examen_T4_y_T5.md"

MODELO = "mistral:instruct"
HOST   = "http://localhost:11434"

# Límite de salida para el examen completo (preguntas + respuestas).
# Si ves que corta, sube a 1800-2400.
NUM_PREDICT_TEST = 1600

# Límite para generar SOLO la hoja de respuestas (segunda llamada).
NUM_PREDICT_KEY  = 650

# Temperatura baja = más obediente al formato
TEMPERATURE = 0.2
# ---------------------------------------------------------


# ---------------------------------------------------------
# 1) PDF -> Markdown (simple)
# ---------------------------------------------------------
def fix_hyphenation(text: str) -> str:
    return re.sub(r"(\w)-\n(\w)", r"\1\2", text)

def normalize_newlines(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text

def is_heading(line: str):
    s = line.strip()
    if not s:
        return None

    if re.match(r"^(tema|unidad|cap[ií]tulo)\s+\d+\b", s, flags=re.IGNORECASE):
        return 1

    if re.match(r"^\d+[\.\)]\s+\S+", s):
        return 2

    if re.match(r"^\d+(\.\d+)+\s+\S+", s):
        dots = s.split()[0].count(".")
        return min(2 + dots, 4)

    if len(s) <= 80 and s.isupper() and not s.endswith(".") and not re.match(r"^[\W\d_]+$", s):
        return 2

    return None

def is_bullet(line: str) -> bool:
    return bool(re.match(r"^\s*[•\-\*]\s+\S+", line))

def pdf_to_md(path_pdf: str, path_md: str) -> str:
    pdf_path = pathlib.Path(path_pdf)
    if not pdf_path.exists():
        print(f"ERROR: No existe el PDF: {path_pdf}", file=sys.stderr)
        sys.exit(1)

    reader = PdfReader(str(pdf_path))

    full_text = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        text = normalize_newlines(text)
        full_text.append(f"\n\n<!-- page: {i} -->\n\n")
        full_text.append(text)

    text = "".join(full_text)
    text = fix_hyphenation(text)

    lines = text.split("\n")
    out = []
    paragraph = []

    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            out.append(" ".join(paragraph).strip())
            out.append("")
            paragraph = []

    for raw in lines:
        line = raw.strip()

        if not line:
            flush_paragraph()
            continue

        lvl = is_heading(line)
        if lvl:
            flush_paragraph()
            out.append("#" * lvl + " " + line)
            out.append("")
            continue

        if is_bullet(line):
            flush_paragraph()
            line = re.sub(r"^\s*[•\-\*]\s+", "- ", line)
            out.append(line)
            continue

        paragraph.append(line)

    flush_paragraph()

    md_text = "\n".join(out)
    md_text = re.sub(r"\n{3,}", "\n\n", md_text).strip() + "\n"

    pathlib.Path(path_md).write_text(md_text, encoding="utf-8")
    return md_text


# ---------------------------------------------------------
# 2) Streaming + barra de progreso
#     ✅ timeout arreglado: timeout=(10, None)
# ---------------------------------------------------------
def _progress_bar(pct: int, width: int = 28) -> str:
    pct = max(0, min(100, pct))
    filled = int(width * pct / 100)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {pct:3d}%"

def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)

def ollama_generate_stream(
    prompt: str,
    *,
    modelo: str = MODELO,
    host: str = HOST,
    num_predict: int = 1200,
    temperature: float = TEMPERATURE,
    etiqueta: str = "Generando"
) -> str:
    url = f"{host}/api/generate"
    payload = {
        "model": modelo.strip(),
        "prompt": prompt,
        "stream": True,
        "options": {
            "num_predict": int(num_predict),
            "temperature": float(temperature),
        }
    }

    try:
        response = requests.post(
            url,
            json=payload,
            stream=True,
            timeout=(10, None)  # ✅ (connect timeout, read timeout sin límite)
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error al contactar con Ollama en {url}: {e}", file=sys.stderr)
        try:
            print(response.text, file=sys.stderr)
        except Exception:
            pass
        sys.exit(1)

    start = time.time()
    chunks = []
    printed_once = False

    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        piece = data.get("response", "")
        if piece:
            chunks.append(piece)

        current_text = "".join(chunks)
        tok = _approx_tokens(current_text)
        pct = int(min(99, tok * 100 / max(1, num_predict)))
        elapsed = time.time() - start

        sys.stdout.write("\r" + f"{etiqueta} " + _progress_bar(pct) + f"  ({elapsed:0.1f}s)")
        sys.stdout.flush()
        printed_once = True

        if data.get("done") is True:
            break

    if printed_once:
        sys.stdout.write("\n")
        sys.stdout.flush()

    return "".join(chunks).strip()


# ---------------------------------------------------------
# 3) Validación + doble prompt (asegurar respuestas)
# ---------------------------------------------------------
def contar_respuestas(md: str) -> int:
    if "## Plantilla de respuestas" not in md:
        return 0
    parte = md.split("## Plantilla de respuestas", 1)[1]
    return len(re.findall(r"(?m)^\s*\d+[\.\)]\s*[ABCD]\b", parte))

def tiene_seccion_respuestas(md: str) -> bool:
    return "## Plantilla de respuestas" in md

def asegurar_hoja_respuestas(examen_md: str, ruta_md_apuntes: str) -> str:
    if tiene_seccion_respuestas(examen_md) and contar_respuestas(examen_md) >= 10:
        return examen_md

    contenido_md = pathlib.Path(ruta_md_apuntes).read_text(encoding="utf-8").strip()

    prompt = dedent(f"""
    Eres corrector de exámenes tipo test.

    Objetivo: generar ÚNICAMENTE la sección de respuestas para un examen de 10 preguntas.

    Reglas:
    - Devuelve SOLO esta sección (nada más):
      ## Plantilla de respuestas
      1. X — justificación breve (1 línea)
      ...
      10. X — justificación breve (1 línea)
    - EXACTAMENTE 10 líneas numeradas del 1 al 10.
    - X debe ser SOLO una letra: A, B, C o D.
    - La letra debe ser coherente con:
      (a) lo que dicen los apuntes y
      (b) el enunciado de la pregunta.
    - Si una pregunta está ambigua (más de una opción parece correcta),
      elige la opción que mejor esté apoyada por los apuntes y explica en 1 línea.

    APUNTES:
    ---
    {contenido_md}
    ---

    EXAMEN:
    ---
    {examen_md}
    ---
    """).strip()

    print("Faltó la hoja de respuestas (o quedó incompleta). Generándola aparte...")
    key = ollama_generate_stream(
        prompt,
        num_predict=NUM_PREDICT_KEY,
        etiqueta="Generando respuestas"
    )

    if "## Plantilla de respuestas" not in key:
        key = "## Plantilla de respuestas\n\n" + key.strip()

    return examen_md.rstrip() + "\n\n" + key.strip() + "\n"


# ---------------------------------------------------------
# 4) Generar examen desde MD (prompt robusto)
# ---------------------------------------------------------
def generar_test_desde_md(ruta_md: str) -> str:
    md_path = pathlib.Path(ruta_md)
    if not md_path.exists():
        print(f"ERROR: No existe el Markdown: {ruta_md}", file=sys.stderr)
        sys.exit(1)

    contenido_md = md_path.read_text(encoding="utf-8").strip()
    if len(contenido_md) < 50:
        print("ERROR: El Markdown parece demasiado corto. ¿El PDF tenía texto seleccionable?", file=sys.stderr)
        sys.exit(1)

    prompt = dedent(f"""
    Eres profesor/a y diseñador/a de exámenes.

    Recibirás apuntes en formato Markdown. Pueden ser de cualquier tema (IPE, marketing, etc.).
    Tu misión es crear un examen tipo test de 10 preguntas.

    REGLAS CRÍTICAS (para evitar preguntas sin sentido):
    1) Basarte SOLO en los apuntes. No uses conocimientos externos ni internet.
    2) Cada pregunta debe tener EXACTAMENTE 1 opción correcta (A/B/C/D).
       - Prohibido hacer preguntas donde 2 o más opciones sean correctas según los apuntes.
       - Si el apunte enumera varias afirmaciones correctas (p.ej. varias fuentes, tipos, pasos),
         NO preguntes “¿cuál es?”.
         En su lugar:
           (a) pregunta “¿Cuál de estas opciones aparece en los apuntes como…?” o
           (b) crea 3 distractores plausibles que NO aparezcan en los apuntes, dejando 1 correcta.
    3) La respuesta correcta debe estar explícitamente apoyada por una frase/idea del apunte.
       Las otras 3 opciones deben ser incorrectas según el apunte (o no mencionadas).
    4) Evita ambigüedad: nada de “todas las anteriores / ninguna de las anteriores”.
    5) Varía el tipo de pregunta (definiciones, relación concepto-ejemplo, condiciones, diferencias, pasos, etc.)
       según el tema del apunte.
    6) Revisión interna ANTES de entregar:
       - Para cada pregunta, verifica: “solo 1 opción es correcta según los apuntes”.
       - Si detectas ambigüedad, reescribe esa pregunta y sus opciones.

    FORMATO DE SALIDA (OBLIGATORIO, en Markdown):
    ## Examen tipo test (10 preguntas)

    1. Pregunta...
       A) ...
       B) ...
       C) ...
       D) ...

    ...
    10. Pregunta...
       A) ...
       B) ...
       C) ...
       D) ...

    ## Plantilla de respuestas
    1. X — breve justificación (1 línea basada en los apuntes)
    2. X — breve justificación (1 línea basada en los apuntes)
    ...
    10. X — breve justificación (1 línea basada en los apuntes)

    APUNTES (Markdown):
    ---
    {contenido_md}
    ---
    """).strip()

    return ollama_generate_stream(
        prompt,
        num_predict=NUM_PREDICT_TEST,
        etiqueta="Generando examen"
    )


# ---------------------------------------------------------
# 5) MAIN
# ---------------------------------------------------------
def main():
    print(f"1) Convirtiendo PDF -> MD: {ARCHIVO_PDF} -> {ARCHIVO_MD}")
    md_text = pdf_to_md(ARCHIVO_PDF, ARCHIVO_MD)
    print(f"OK: Markdown generado ({len(md_text)} chars).")

    print(f"2) Generando examen con IA local (Ollama) usando modelo: {MODELO}")
    examen = generar_test_desde_md(ARCHIVO_MD)

    # Doble prompt: si faltan respuestas, generarlas aparte
    examen = asegurar_hoja_respuestas(examen, ARCHIVO_MD)

    pathlib.Path(ARCHIVO_TEST).write_text(examen + "\n", encoding="utf-8")
    print(f"OK: Examen guardado en: {ARCHIVO_TEST}\n")

    print(examen)


if __name__ == "__main__":
    main()
