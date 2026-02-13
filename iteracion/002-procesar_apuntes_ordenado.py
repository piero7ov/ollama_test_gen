#!/usr/bin/env python3
# ============================================================
# 001_procesar_apuntes.py
# ------------------------------------------------------------
# - Lee un PDF con texto seleccionable
# - Extrae el texto por páginas
# - Aplica un "formato" básico para Markdown:
#     * Detecta títulos por patrones comunes (Tema, Unidad, 1., 1.1, etc.)
#     * Detecta listas con viñetas (•, -, *)
#     * Une líneas en párrafos para que no quede 1 línea por renglón
#     * Arregla palabras cortadas por guión al salto de línea (auto-\nmático)
# - Guarda el resultado en un .md
# ============================================================

import re
from pypdf import PdfReader


# ------------------------------------------------------------
# Helpers de limpieza (mínimos)
# ------------------------------------------------------------

def fix_hyphenation(text: str) -> str:
    """
    Junta palabras cortadas con guión al final de línea:
      "auto-\nmatización" -> "automatización"
    """
    # Solo junta si el guión está pegado a una letra y la siguiente línea empieza con letra.
    return re.sub(r"(\w)-\n(\w)", r"\1\2", text)


def normalize_newlines(text: str) -> str:
    """
    Normaliza saltos de línea y espacios.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Quita espacios al final de línea
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text


def is_heading(line: str):
    """
    Detecta si una línea parece título y qué nivel de heading usar.

    Reglas simples:
    - "TEMA 1 ..." o "Tema 1 ..." -> H1
    - "UNIDAD 2 ..." -> H1
    - "1. ..." -> H2
    - "1.1 ..." -> H3
    - Linea en MAYÚSCULAS corta (sin punto final) -> H2
    """
    s = line.strip()
    if not s:
        return None

    # Tema/Unidad/Capítulo como títulos principales
    if re.match(r"^(tema|unidad|cap[ií]tulo)\s+\d+\b", s, flags=re.IGNORECASE):
        return 1

    # Numeración tipo 1. o 2) como secciones
    if re.match(r"^\d+[\.\)]\s+\S+", s):
        return 2

    # Numeración tipo 1.1 / 2.3.4 como subsecciones
    if re.match(r"^\d+(\.\d+)+\s+\S+", s):
        # nivel según cantidad de puntos (máx H4)
        dots = s.split()[0].count(".")
        return min(2 + dots, 4)

    # Mayúsculas “tipo título”
    if (
        len(s) <= 80
        and s.isupper()
        and not s.endswith(".")
        and not re.match(r"^[\W\d_]+$", s)  # no solo símbolos/números
    ):
        return 2

    return None


def is_bullet(line: str) -> bool:
    """
    Detecta viñetas simples:
      • item
      - item
      * item
    """
    return bool(re.match(r"^\s*[•\-\*]\s+\S+", line))


# ------------------------------------------------------------
# Conversión principal PDF -> Markdown
# ------------------------------------------------------------

def pdf_to_md(path_pdf: str, path_md: str) -> str:
    reader = PdfReader(path_pdf)

    # 1) Extraer texto de todas las páginas
    full_text = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        text = normalize_newlines(text)

        # Separador de página (opcional pero útil en apuntes)
        # Si no lo quieres, borra estas 2 líneas.
        full_text.append(f"\n\n<!-- page: {i} -->\n\n")
        full_text.append(text)

    text = "".join(full_text)
    text = fix_hyphenation(text)

    # 2) Convertir a Markdown con reglas simples
    lines = text.split("\n")

    out = []
    paragraph = []

    def flush_paragraph():
        """Vuelca el párrafo acumulado a salida."""
        nonlocal paragraph
        if paragraph:
            out.append(" ".join(paragraph).strip())
            out.append("")  # línea en blanco
            paragraph = []

    for raw in lines:
        line = raw.strip()

        # Línea vacía -> corta párrafo
        if not line:
            flush_paragraph()
            continue

        # Heading
        lvl = is_heading(line)
        if lvl:
            flush_paragraph()
            out.append("#" * lvl + " " + line)
            out.append("")
            continue

        # Lista con viñetas
        if is_bullet(line):
            flush_paragraph()
            # Normaliza cualquier viñeta a "- "
            line = re.sub(r"^\s*[•\-\*]\s+", "- ", line)
            out.append(line)
            continue

        # Si no es heading/lista, lo tratamos como texto normal (párrafo)
        paragraph.append(line)

    flush_paragraph()

    # Limpieza final: evita demasiados saltos de línea seguidos
    md_text = "\n".join(out)
    md_text = re.sub(r"\n{3,}", "\n\n", md_text).strip() + "\n"

    # 3) Guardar
    with open(path_md, "w", encoding="utf-8") as f:
        f.write(md_text)

    return md_text
# ============================================================

pdf_to_md("Apuntes_T3_y_T4.pdf", "Apuntes_T3_y_T4.md")
print("OK -> apuntes.md generado.")

