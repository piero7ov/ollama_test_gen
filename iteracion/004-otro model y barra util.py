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
# ⭐ CONFIG
# ---------------------------------------------------------
ARCHIVO_PDF  = "Apuntes_T4_y_T5.pdf"
ARCHIVO_MD   = "Apuntes_T4_y_T5.md"
ARCHIVO_TEST = "Examen_T4_y_T5.md"

# Prueba estos modelos (los que tienes):
#   - "llama3:latest"         (suele ser el más rápido y bueno en texto general)
#   - "qwen2.5-coder:7b"      (sirve, pero es más “coder”)
#   - "deepseek-r1:latest"    (suele razonar mejor, pero va más lento)
MODELO = "llama3:latest"
HOST   = "http://localhost:11434"

# Límite de salida. Esto permite estimar porcentaje.
# Para 10 preguntas + respuestas suele ir bien 800–1400.
NUM_PREDICT = 1200
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
# 2) Ollama streaming + progreso (porcentaje estimado)
# ---------------------------------------------------------
def _progress_bar(pct: int, width: int = 28) -> str:
    pct = max(0, min(100, pct))
    filled = int(width * pct / 100)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {pct:3d}%"

def _approx_tokens(text: str) -> int:
    """
    Aproximación rápida de tokens:
    - Muchos modelos rondan ~4 caracteres por token (aprox).
    No es exacto, pero sirve para % estimado.
    """
    return max(1, len(text) // 4)

def generar_test_desde_md(ruta_md: str, modelo: str = MODELO, host: str = HOST) -> str:
    md_path = pathlib.Path(ruta_md)
    if not md_path.exists():
        print(f"ERROR: No existe el Markdown: {ruta_md}", file=sys.stderr)
        sys.exit(1)

    contenido_md = md_path.read_text(encoding="utf-8").strip()
    if len(contenido_md) < 50:
        print("ERROR: El Markdown parece demasiado corto. ¿El PDF tenía texto seleccionable?", file=sys.stderr)
        sys.exit(1)

    prompt = dedent(f"""
    Eres profesor y diseñador de exámenes.

    Recibirás apuntes en formato Markdown.

    Tu tarea:
    - Crear un EXAMEN TIPO TEST de 10 preguntas basadas ÚNICAMENTE en el contenido de los apuntes.
    - Cada pregunta debe tener 4 opciones: A, B, C, D.
    - Debe haber solo 1 opción correcta por pregunta.
    - Dificultad: media a alta (sin inventar cosas fuera del texto).
    - Evita preguntas ambiguas.
    - No copies frases largas literalmente; redacta natural.

    Formato de salida (OBLIGATORIO, en Markdown):
    ## Examen tipo test (10 preguntas)
    1. Pregunta...
       A) ...
       B) ...
       C) ...
       D) ...
    ...
    ## Plantilla de respuestas
    1. X — breve justificación (1 línea)
    2. X — breve justificación (1 línea)
    ...

    APUNTES (Markdown):
    ---
    {contenido_md}
    ---
    """).strip()

    url = f"{host}/api/generate"
    payload = {
        "model": modelo.strip(),
        "prompt": prompt,
        "stream": True,  # ✅ streaming para ver avance
        "options": {
            "num_predict": int(NUM_PREDICT),  # ✅ límite de salida (para %)
        }
    }

    try:
        # stream=True en requests para iterar por líneas (JSONL)
        response = requests.post(url, json=payload, stream=True, timeout=600)
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

    # Ollama devuelve JSON por líneas (una por “chunk”)
    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        # trocito de texto generado
        piece = data.get("response", "")
        if piece:
            chunks.append(piece)

        # progreso estimado
        current_text = "".join(chunks)
        tok = _approx_tokens(current_text)
        pct = int(min(99, tok * 100 / max(1, NUM_PREDICT)))  # hasta 99% mientras no termine
        elapsed = time.time() - start

        # imprimir barra “en la misma línea”
        sys.stdout.write("\r" + "Generando examen " + _progress_bar(pct) + f"  ({elapsed:0.1f}s)")
        sys.stdout.flush()
        printed_once = True

        # fin
        if data.get("done") is True:
            break

    # salto de línea para no dejar el cursor pegado
    if printed_once:
        sys.stdout.write("\n")
        sys.stdout.flush()

    result = "".join(chunks).strip()
    return result


# ---------------------------------------------------------
# 3) MAIN
# ---------------------------------------------------------
def main():
    print(f"1) Convirtiendo PDF -> MD: {ARCHIVO_PDF} -> {ARCHIVO_MD}")
    md_text = pdf_to_md(ARCHIVO_PDF, ARCHIVO_MD)
    print(f"OK: Markdown generado ({len(md_text)} chars).")

    print(f"2) Generando examen con IA local (Ollama) usando modelo: {MODELO}")
    examen = generar_test_desde_md(ARCHIVO_MD)

    pathlib.Path(ARCHIVO_TEST).write_text(examen + "\n", encoding="utf-8")
    print(f"OK: Examen guardado en: {ARCHIVO_TEST}\n")

    print(examen)


if __name__ == "__main__":
    main()
