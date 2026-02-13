from pypdf import PdfReader
from markdownify import markdownify as md

def pdf_to_md(path_pdf, path_md):
    reader = PdfReader(path_pdf)
    full_text = ""

    for page in reader.pages:
        text = page.extract_text() or ""
        full_text += text + "\n\n"

    # Convertir texto a Markdown (markdownify trabaja mejor con HTML,
    # pero en textos normales también funciona)
    md_text = md(full_text)

    with open(path_md, "w", encoding="utf-8") as f:
        f.write(md_text)

    return md_text

# Ejemplo
pdf_to_md("Apuntes_T3_y_T4.pdf", "Apuntes_T3_y_T4.md")

