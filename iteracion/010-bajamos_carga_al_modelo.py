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
ARCHIVO_PDF  = "Apuntes_T3_y_profesiograma.pdf"
ARCHIVO_MD   = "Apuntes_T3_y_profesiograma.md"
ARCHIVO_TEST = "Examen_T3_y_profesiograma.md"

# Prueba:
MODELO = "qwen2.5-coder:7b"
# MODELO = "mistral:instruct"

HOST   = "http://localhost:11434"

# Para 8 preguntas, normalmente con 500-900 sobra.
NUM_PREDICT = 900

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
# 2) Streaming + barra de progreso + Ctrl+C cancela
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
    num_predict: int = NUM_PREDICT,
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

    response = None
    chunks = []
    printed_once = False
    start = None

    try:
        response = requests.post(url, json=payload, stream=True, timeout=(10, None))
        response.raise_for_status()
        start = time.time()

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

    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
        print("⛔ Cancelado por el usuario (Ctrl+C). Cerrando conexión con Ollama...")
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
        raise

    except requests.exceptions.RequestException as e:
        sys.stdout.write("\n")
        sys.stdout.flush()
        print(f"Error al contactar con Ollama en {url}: {e}", file=sys.stderr)
        try:
            if response is not None:
                print(response.text, file=sys.stderr)
        except Exception:
            pass
        sys.exit(1)

    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass

    if printed_once:
        sys.stdout.write("\n")
        sys.stdout.flush()

    return "".join(chunks).strip()


# ---------------------------------------------------------
# 3) Generar examen (1 prompt, V/F + corta)
# ---------------------------------------------------------
def generar_examen_vf_y_corta(ruta_md: str) -> str:
    md_path = pathlib.Path(ruta_md)
    if not md_path.exists():
        print(f"ERROR: No existe el Markdown: {ruta_md}", file=sys.stderr)
        sys.exit(1)

    contenido_md = md_path.read_text(encoding="utf-8").strip()
    if len(contenido_md) < 50:
        print("ERROR: El Markdown parece demasiado corto. ¿El PDF tenía texto seleccionable?", file=sys.stderr)
        sys.exit(1)

    prompt = dedent(f"""
    Eres profesor/a.

    A partir de estos apuntes, crea un mini-examen con:
    - 4 preguntas de VERDADERO/FALSO
    - 4 preguntas de RESPUESTA CORTA (1 frase)

    REGLAS:
    - Basarte SOLO en los apuntes (no uses internet ni inventes).
    - Las preguntas deben tener sentido y estar apoyadas por el texto.
    - Evita ambigüedad.
    - Las respuestas deben ser claras y cortas.

    FORMATO OBLIGATORIO (respeta tal cual):
    ## Examen (V/F y respuesta corta)

    ### Verdadero o falso
    1. (V/F) ...
    2. (V/F) ...
    3. (V/F) ...
    4. (V/F) ...

    ### Respuesta corta
    5. ...
    6. ...
    7. ...
    8. ...

    ## Respuestas
    1. V
    2. F
    3. V
    4. F
    5. (respuesta corta)
    6. (respuesta corta)
    7. (respuesta corta)
    8. (respuesta corta)

    APUNTES:
    ---
    {contenido_md}
    ---
    """).strip()

    return ollama_generate_stream(
        prompt,
        etiqueta="Generando examen"
    )


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    try:
        print(f"1) Convirtiendo PDF -> MD: {ARCHIVO_PDF} -> {ARCHIVO_MD}")
        md_text = pdf_to_md(ARCHIVO_PDF, ARCHIVO_MD)
        print(f"OK: Markdown generado ({len(md_text)} chars).")

        print(f"2) Generando mini-examen (V/F + respuesta corta) | modelo: {MODELO}")
        examen = generar_examen_vf_y_corta(ARCHIVO_MD)

        pathlib.Path(ARCHIVO_TEST).write_text(examen + "\n", encoding="utf-8")
        print(f"OK: Examen guardado en: {ARCHIVO_TEST}\n")

        print(examen)

    except KeyboardInterrupt:
        print("\n✅ Proceso cancelado. No se guardó el examen.")
        sys.exit(130)


if __name__ == "__main__":
    main()
