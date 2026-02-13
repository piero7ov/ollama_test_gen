#!/usr/bin/env python3
import pathlib
import sys
import re
import requests
from textwrap import dedent
from pypdf import PdfReader

# ---------------------------------------------------------
#  CONFIGURACION
# ---------------------------------------------------------
ARCHIVO_PDF  = "Apuntes_T4_y_T5.pdf"
ARCHIVO_MD   = "Apuntes_T4_y_T5.md"
ARCHIVO_TEST = "examen_T4_y_T5.md"

MODELO = "qwen2.5-coder:7b"       
HOST   = "http://localhost:11434" 
# ---------------------------------------------------------


# ---------------------------------------------------------
# 1) PDF -> Markdown 
# ---------------------------------------------------------
def fix_hyphenation(text: str) -> str:
    """Junta palabras cortadas con guión al salto de línea: auto-\nmatización -> automatización"""
    return re.sub(r"(\w)-\n(\w)", r"\1\2", text)

def normalize_newlines(text: str) -> str:
    """Normaliza saltos de línea y quita espacios raros al final de línea."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text

def is_heading(line: str):
    """
    Detección muy básica de títulos para apuntes:
    - "Tema 1", "Unidad 2", "Capítulo 3" => H1
    - "1. ..." o "1) ..." => H2
    - "1.1 ..." => H3/H4
    - MAYÚSCULAS cortas (sin punto) => H2
    """
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
    """Detecta viñetas simples: • item / - item / * item"""
    return bool(re.match(r"^\s*[•\-\*]\s+\S+", line))

def pdf_to_md(path_pdf: str, path_md: str) -> str:
    """
    Extrae texto del PDF con pypdf y lo formatea a un Markdown básico.
    """
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
# 2) Markdown -> IA local (Ollama) para generar test
# ---------------------------------------------------------
def generar_test_desde_md(
    ruta_md: str,
    modelo: str = MODELO,
    host: str = HOST
) -> str:
    """
    Lee el Markdown, lo envía a Ollama (/api/generate) y devuelve un examen tipo test.
    """
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
    - Dificultad: media a alta (pero sin inventar cosas fuera del texto).
    - Evita preguntas ambiguas.
    - No repitas literalmente frases largas; redacta de forma natural.

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
        "stream": False
    }

    try:
        response = requests.post(url, json=payload, timeout=600)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error al contactar con Ollama en {url}: {e}", file=sys.stderr)
        if 'response' in locals() and response is not None:
            try:
                print("Cuerpo devuelto por el servidor:", file=sys.stderr)
                print(response.text, file=sys.stderr)
            except Exception:
                pass
        sys.exit(1)

    data = response.json()

    try:
        return data["response"].strip()
    except (KeyError, TypeError):
        print("Respuesta inesperada de Ollama:", file=sys.stderr)
        print(data, file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------
# 3) MAIN: todo el flujo
# ---------------------------------------------------------
def main():
    print(f"1) Convirtiendo PDF -> MD: {ARCHIVO_PDF} -> {ARCHIVO_MD}")
    md_text = pdf_to_md(ARCHIVO_PDF, ARCHIVO_MD)
    print(f"OK: Markdown generado ({len(md_text)} chars).")

    print("2) Generando examen con IA local (Ollama)...")
    examen = generar_test_desde_md(ARCHIVO_MD)

    pathlib.Path(ARCHIVO_TEST).write_text(examen + "\n", encoding="utf-8")
    print(f"OK: Examen guardado en: {ARCHIVO_TEST}\n")

    # También lo imprimimos
    print(examen)


if __name__ == "__main__":
    main()
