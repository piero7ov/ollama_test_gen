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
# ---------------------------------------------------------
ARCHIVO_PDF  = "Apuntes_T4_y_T5.pdf"
ARCHIVO_MD   = "Apuntes_T4_y_T5.md"
ARCHIVO_TEST = "Examen_T4_y_T5.md"

MODELO = "deepseek-r1:latest"
HOST   = "http://localhost:11434"

# Salida para el examen y para la revisión + respuestas
NUM_PREDICT_EXAM   = 1400
NUM_PREDICT_REPAIR = 1200

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
# 2) Streaming + barra de progreso + timeout sin límite
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
            timeout=(10, None)  # connect timeout, read timeout ilimitado
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
# 3) Validadores de formato (clave para que no se "raye")
# ---------------------------------------------------------
def _get_exam_section(md: str) -> str:
    if "## Examen tipo test" not in md:
        return ""
    part = md.split("## Examen tipo test", 1)[1]
    if "## Plantilla de respuestas" in part:
        part = part.split("## Plantilla de respuestas", 1)[0]
    return part.strip()

def _get_answers_section(md: str) -> str:
    if "## Plantilla de respuestas" not in md:
        return ""
    return md.split("## Plantilla de respuestas", 1)[1].strip()

def validar_examen_con_opciones(md: str) -> bool:
    """
    Examen válido si:
    - hay 10 preguntas (1..10)
    - hay 40 opciones (A,B,C,D por pregunta)
    - las preguntas NO son "1. A) ..." (o sea, no empieza la pregunta con A))
    """
    exam = _get_exam_section(md)
    if not exam:
        return False

    # Preguntas 1..10
    q_nums = re.findall(r"(?m)^\s*(\d{1,2})\.\s+(.+)$", exam)
    if len(q_nums) < 10:
        return False

    # Evitar que la "pregunta" sea realmente una opción: "1. A) ..."
    for n, txt in q_nums[:10]:
        if re.match(r"^\s*[ABCD]\)\s+", txt.strip()):
            return False

    # Contar opciones totales dentro del examen
    options = re.findall(r"(?m)^\s*[ABCD]\)\s+\S+", exam)
    if len(options) < 40:
        return False

    # Comprobar que existen 1..10 al menos una vez
    nums_present = {int(n) for n, _ in q_nums}
    if not all(i in nums_present for i in range(1, 11)):
        return False

    return True

def contar_respuestas_letras(md: str) -> int:
    ans = _get_answers_section(md)
    if not ans:
        return 0
    return len(re.findall(r"(?m)^\s*\d+[\.\)]\s*[ABCD]\b", ans))

def validar_respuestas_10(md: str) -> bool:
    return ("## Plantilla de respuestas" in md) and (contar_respuestas_letras(md) >= 10)


# ---------------------------------------------------------
# 4) Paso 1: generar examen (solo preguntas)
# ---------------------------------------------------------
def generar_examen_sin_respuestas(ruta_md: str, *, strict: bool = False) -> str:
    contenido_md = pathlib.Path(ruta_md).read_text(encoding="utf-8").strip()
    if len(contenido_md) < 50:
        print("ERROR: El Markdown parece demasiado corto. ¿El PDF tenía texto seleccionable?", file=sys.stderr)
        sys.exit(1)

    extra = ""
    if strict:
        extra = """
    REGLA EXTRA (estricta):
    - Cada pregunta DEBE ser una frase completa y NO puede empezar por "A)" ni "B)".
    - Debes escribir SIEMPRE 4 líneas de opciones (A/B/C/D) debajo de cada pregunta.
    - Si no puedes cumplir el formato, responde EXACTAMENTE: FORMAT_ERROR
        """.rstrip()

    prompt = dedent(f"""
    Eres profesor/a y diseñador/a de exámenes.

    Crea SOLO el examen tipo test de 10 preguntas, basado SOLO en los apuntes.
    (No uses internet ni conocimientos externos.)

    REGLAS CLAVE:
    - EXACTAMENTE 10 preguntas (1..10).
    - Cada pregunta tiene EXACTAMENTE 1 opción correcta.
    - Si el apunte enumera varias cosas correctas, NO preguntes “¿cuál es?”.
      Mejor:
        (a) "¿Cuál de estas opciones aparece en los apuntes como...?" (con 3 distractores NO mencionados)
        o
        (b) "¿Cuál NO aparece en los apuntes...?" (la correcta es la NO mencionada)
    - NO uses "todas las anteriores" / "ninguna de las anteriores".

    {extra}

    FORMATO OBLIGATORIO:
    ## Examen tipo test (10 preguntas)

    1. Pregunta (frase completa, no empieza con A))
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

    APUNTES:
    ---
    {contenido_md}
    ---
    """).strip()

    temp = 0.0 if strict else TEMPERATURE
    return ollama_generate_stream(prompt, num_predict=NUM_PREDICT_EXAM, temperature=temp, etiqueta="Generando examen")


# ---------------------------------------------------------
# 5) Paso 2: revisar + reparar + respuestas SOLO letras
# ---------------------------------------------------------
def revisar_reparar_y_responder(examen_md: str, ruta_md_apuntes: str, *, strict: bool = False) -> str:
    apuntes = pathlib.Path(ruta_md_apuntes).read_text(encoding="utf-8").strip()

    extra = ""
    if strict:
        extra = """
    REGLA EXTRA (estricta):
    - La sección de examen debe tener 10 preguntas y cada una con 4 opciones A/B/C/D.
    - La sección de respuestas debe tener EXACTAMENTE 10 líneas: "1. A" ... "10. D".
    - Si no puedes cumplir, responde EXACTAMENTE: FORMAT_ERROR
        """.rstrip()

    prompt = dedent(f"""
    Eres revisor/a de exámenes tipo test.

    Tienes APUNTES y un EXAMEN.
    Tu tarea:
    1) Revisar y reparar cualquier pregunta ambigua (si 2 opciones podrían ser correctas según apuntes).
       Debes dejar cada pregunta con EXACTAMENTE 1 opción correcta.
       Haz cambios mínimos (enunciado u opciones) manteniendo el tema.
    2) Generar la plantilla de respuestas SOLO con letras.

    {extra}

    SALIDA OBLIGATORIA (solo estas 2 secciones):
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
    1. X
    2. X
    ...
    10. X

    (X es solo A/B/C/D, sin explicación.)

    APUNTES:
    ---
    {apuntes}
    ---

    EXAMEN:
    ---
    {examen_md}
    ---
    """).strip()

    temp = 0.0 if strict else TEMPERATURE
    return ollama_generate_stream(prompt, num_predict=NUM_PREDICT_REPAIR, temperature=temp, etiqueta="Revisando + respuestas")


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    print(f"1) Convirtiendo PDF -> MD: {ARCHIVO_PDF} -> {ARCHIVO_MD}")
    md_text = pdf_to_md(ARCHIVO_PDF, ARCHIVO_MD)
    print(f"OK: Markdown generado ({len(md_text)} chars).")

    print(f"2) Generando examen (solo preguntas) | modelo: {MODELO}")
    exam = generar_examen_sin_respuestas(ARCHIVO_MD, strict=False)

    # ✅ Reintento si salió mal (como tu caso "1. A) ...")
    if exam.strip() == "FORMAT_ERROR" or not validar_examen_con_opciones(exam):
        print("AVISO: El examen salió con formato incorrecto. Reintentando en modo estricto...")
        exam = generar_examen_sin_respuestas(ARCHIVO_MD, strict=True)

    # Si aún falla, guardamos lo que hay para depurar
    if exam.strip() == "FORMAT_ERROR" or not validar_examen_con_opciones(exam):
        print("ERROR: El modelo sigue sin respetar el formato del examen. Guardando salida para depurar.", file=sys.stderr)
        pathlib.Path(ARCHIVO_TEST).write_text(exam + "\n", encoding="utf-8")
        print(f"Salida guardada en: {ARCHIVO_TEST}")
        sys.exit(1)

    print("3) Reparando ambigüedades + generando respuestas (solo letras)")
    final = revisar_reparar_y_responder(exam, ARCHIVO_MD, strict=False)

    if final.strip() == "FORMAT_ERROR" or (not validar_examen_con_opciones(final)) or (not validar_respuestas_10(final)):
        print("AVISO: Salida final con formato incorrecto. Reintentando revisión en modo estricto...")
        final = revisar_reparar_y_responder(exam, ARCHIVO_MD, strict=True)

    if final.strip() == "FORMAT_ERROR" or (not validar_examen_con_opciones(final)) or (not validar_respuestas_10(final)):
        print("ERROR: La salida final sigue sin formato válido. Guardando para depurar.", file=sys.stderr)
        pathlib.Path(ARCHIVO_TEST).write_text(final + "\n", encoding="utf-8")
        print(f"Salida guardada en: {ARCHIVO_TEST}")
        sys.exit(1)

    pathlib.Path(ARCHIVO_TEST).write_text(final + "\n", encoding="utf-8")
    print(f"OK: Examen guardado en: {ARCHIVO_TEST}\n")
    print(final)


if __name__ == "__main__":
    main()
