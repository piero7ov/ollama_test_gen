#!/usr/bin/env python3
# ==========================================================
#  Generador de Exámenes (GUI) — PDF -> Markdown -> Ollama
# ==========================================================
#  ¿Qué hace este script?
#  ----------------------------------------------------------
#  Este programa ofrece una interfaz gráfica para:
#
#   1) Seleccionar un PDF de apuntes.
#   2) Convertirlo a Markdown de forma simple (títulos, listas, párrafos).
#   3) Enviar los apuntes en Markdown a una IA local (Ollama) para generar:
#       - Preguntas Verdadero/Falso
#       - Preguntas de Respuesta corta
#     según el número que el usuario indique.
#   4) Guardar el examen generado en un archivo .md en la carpeta de salida.
#   5) (Opcional) Archivar el PDF original en /archivados.
#
#  Importante:
#  - El PDF debe tener texto "seleccionable" (no solo imágenes).
#    Si el PDF es escaneado, extract_text() puede devolver vacío.
#  - Ollama debe estar corriendo en http://localhost:11434
#
#  Cancelación:
#  - El botón "Cancelar" detiene el streaming del modelo (corta la petición)
#    usando un threading.Event.
#
#  UI:
#  - Si tienes ttkbootstrap instalado, la interfaz soporta temas (themes).
#  - Si no lo tienes, funciona igual con tkinter/ttk estándar.
# ==========================================================

import pathlib
import sys
import re
import json
import time
import shutil
import threading
import queue
import requests
from datetime import datetime
from textwrap import dedent
from pypdf import PdfReader

import tkinter as tk
from tkinter import filedialog, messagebox

# ==========================================================
# ✅ UI THEMES (ttkbootstrap)
# ----------------------------------------------------------
# Instalar:
#   py -m pip install ttkbootstrap
#
# Temas populares para probar (ttkbootstrap):
#   "superhero"  (dark, azul)
#   "darkly"     (dark)
#   "cyborg"     (dark)
#   "solar"      (dark)
#   "vapor"      (dark/pastel)
#   "flatly"     (light limpio)
#   "litera"     (light)
#   "minty"      (light)
#   "cosmo"      (light)
#   "pulse"      (light)
#   "sandstone"  (light)
#   "yeti"       (light)
#   "journal"    (light)
# ==========================================================

TTKBOOTSTRAP_AVAILABLE = False
try:
    import ttkbootstrap as tb
    import tkinter.ttk as ttk   # ✅ ttk siempre es de tkinter (compatible)
    TTKBOOTSTRAP_AVAILABLE = True
except Exception:
    tb = None                  # por si el resto del código referencia tb
    import tkinter.ttk as ttk  # fallback estándar


# ============================
#  CONFIG BASE
# ============================
# Host por defecto de Ollama
DEFAULT_HOST = "http://localhost:11434"

# Modelos que quieres ofrecer en el combo (los puedes editar libremente)
MODELOS_DISPONIBLES = [
    "qwen2.5-coder:7b",
    "mistral:instruct",
    "llama3:latest",
    "deepseek-r1:latest",
]

# Límite de preguntas en total (suma V/F + respuesta corta)
MAX_PREGUNTAS = 10

# Parámetros por defecto para /api/generate
DEFAULT_NUM_PREDICT = 950
DEFAULT_TEMPERATURE = 0.2

# Tema por defecto (solo aplica si ttkbootstrap está instalado)
DEFAULT_THEME = "flatly"


# ============================
#  Helpers GUI (seguro)
# ============================
def safe_int(value: str, default: int = 0) -> int:
    """
    Convierte un string a int de forma segura para inputs de GUI.

    Motivo:
    - En tkinter, el usuario puede borrar el contenido o escribir letras.
    - IntVar/Spinbox a veces revienta si hay "" o "e".
    - Con safe_int evitamos errores tipo TclError.

    Ejemplos:
    - "" -> default
    - "e" -> default
    - "  5 " -> 5
    - "-3" -> default (no aceptamos negativos)
    """
    try:
        s = (value or "").strip()
        if s == "":
            return default
        n = int(s)
        if n < 0:
            return default
        return n
    except Exception:
        return default


# ============================
#  PDF -> Markdown (simple)
# ============================
def fix_hyphenation(text: str) -> str:
    """
    Une palabras cortadas por guión al final de línea.
    Ej: "acti-\nvidad" -> "actividad"
    """
    return re.sub(r"(\w)-\n(\w)", r"\1\2", text)

def normalize_newlines(text: str) -> str:
    """
    Normaliza saltos de línea y limpia espacios al final de línea.
    Esto ayuda a reconstruir párrafos.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text

def is_heading(line: str):
    """
    Heurística simple para detectar posibles títulos y convertirlos a Markdown.
    Devuelve nivel (1..4) o None.
    """
    s = line.strip()
    if not s:
        return None

    # "Tema 4", "Unidad 2", "Capítulo 3"
    if re.match(r"^(tema|unidad|cap[ií]tulo)\s+\d+\b", s, flags=re.IGNORECASE):
        return 1

    # "1) Algo" o "1. Algo"
    if re.match(r"^\d+[\.\)]\s+\S+", s):
        return 2

    # "1.2.3 Algo"
    if re.match(r"^\d+(\.\d+)+\s+\S+", s):
        dots = s.split()[0].count(".")
        return min(2 + dots, 4)

    # Línea en mayúsculas corta, estilo encabezado
    if len(s) <= 80 and s.isupper() and not s.endswith(".") and not re.match(r"^[\W\d_]+$", s):
        return 2

    return None

def is_bullet(line: str) -> bool:
    """
    Detecta viñetas típicas: • - *
    """
    return bool(re.match(r"^\s*[•\-\*]\s+\S+", line))

def pdf_to_md(path_pdf: str, path_md: str) -> str:
    """
    Convierte un PDF a Markdown sencillo.

    - Extrae el texto página por página con pypdf.
    - Inserta comentarios <!-- page: N --> para mantener referencia.
    - Reconstituye párrafos (une líneas) y convierte:
      - títulos detectados -> #, ##, ### ...
      - bullets -> "- item"

    Si el PDF no tiene texto seleccionable (escaneado), puede salir muy corto.
    """
    pdf_path = pathlib.Path(path_pdf)
    if not pdf_path.exists():
        raise FileNotFoundError(f"No existe el PDF: {path_pdf}")

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
        """Vuelca el párrafo acumulado en una sola línea (Markdown)."""
        nonlocal paragraph
        if paragraph:
            out.append(" ".join(paragraph).strip())
            out.append("")
            paragraph = []

    for raw in lines:
        line = raw.strip()

        # línea vacía => cortar párrafo
        if not line:
            flush_paragraph()
            continue

        # encabezado detectado
        lvl = is_heading(line)
        if lvl:
            flush_paragraph()
            out.append("#" * lvl + " " + line)
            out.append("")
            continue

        # bullet detectado
        if is_bullet(line):
            flush_paragraph()
            line = re.sub(r"^\s*[•\-\*]\s+", "- ", line)
            out.append(line)
            continue

        # línea normal => acumular
        paragraph.append(line)

    flush_paragraph()

    md_text = "\n".join(out)
    md_text = re.sub(r"\n{3,}", "\n\n", md_text).strip() + "\n"
    pathlib.Path(path_md).write_text(md_text, encoding="utf-8")
    return md_text


# ============================
#  Ollama streaming + cancel
# ============================
class CancelledByUser(Exception):
    """
    Excepción interna para cortar el proceso cuando el usuario pulsa "Cancelar".
    """
    pass

def ollama_generate_stream(
    prompt: str,
    *,
    model: str,
    host: str,
    num_predict: int,
    temperature: float,
    cancel_event: threading.Event,
    on_progress=None,
) -> str:
    """
    Llama a Ollama /api/generate en modo streaming (stream=True).

    Ventajas:
    - Podemos ir actualizando el tiempo transcurrido.
    - Podemos cancelar sin esperar a que termine todo.

    cancel_event:
    - Si el usuario pulsa Cancelar, se activa el event y cortamos.

    on_progress:
    - Callback opcional: on_progress(texto_actual, elapsed_seconds)
      útil para mostrar tiempo en GUI.
    """
    url = f"{host}/api/generate"
    payload = {
        "model": model.strip(),
        "prompt": prompt,
        "stream": True,
        "options": {
            "num_predict": int(num_predict),
            "temperature": float(temperature),
        }
    }

    response = None
    chunks = []
    start = None

    try:
        # timeout=(connect_timeout, read_timeout)
        # read_timeout=None => sin límite (evita "Read timed out" en modelos lentos)
        response = requests.post(url, json=payload, stream=True, timeout=(10, None))
        response.raise_for_status()

        start = time.time()

        # Ollama envía JSON por líneas (JSONL)
        for line in response.iter_lines(decode_unicode=True):
            # Permite cancelar durante el stream
            if cancel_event.is_set():
                raise CancelledByUser()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # trozo del texto generado
            piece = data.get("response", "")
            if piece:
                chunks.append(piece)

            # informar progreso (tiempo)
            if on_progress:
                elapsed = time.time() - start
                on_progress("".join(chunks), elapsed)

            # fin del streaming
            if data.get("done") is True:
                break

        return "".join(chunks).strip()

    except CancelledByUser:
        # Cerramos conexión para no dejarla colgada
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
        raise

    finally:
        # Asegurar cierre del response pase lo que pase
        try:
            if response is not None:
                response.close()
        except Exception:
            pass


# ============================
#  Prompt builder (corto)
# ============================
def build_prompt(apuntes_md: str, n_vf: int, n_short: int) -> str:
    """
    Construye un prompt breve (para que sea rápido) pero con directrices claras:
    - Repartir preguntas entre diferentes secciones/temas del texto.
    - Evitar meter pistas obvias en el enunciado de respuesta corta.
    - Mantener formato fijo para poder validar.

    Formato objetivo:
    ## Examen
    ### Verdadero o falso
    1. (V/F) ...
    ...
    ### Respuesta corta
    5. ¿...?
    ...
    ## Respuestas
    1. V
    2. F
    ...
    10. ...
    """
    total = n_vf + n_short

    # Descripción del "formato" en texto para guiar al modelo
    sections = ["## Examen"]

    if n_vf > 0:
        sections.append("### Verdadero o falso")
        sections.append(f"- Escribe {n_vf} enunciados numerados del 1 al {n_vf}.")
        sections.append("- Cada línea debe empezar con: `N. (V/F) ...` (obligatorio).")

    if n_short > 0:
        start = 1 if n_vf == 0 else (n_vf + 1)
        end = total
        sections.append("### Respuesta corta")
        sections.append(f"- Escribe {n_short} preguntas numeradas del {start} al {end}.")
        sections.append("- Deben ser preguntas reales (idealmente con `?`).")
        sections.append("- No copies frases literales del apunte ni metas pistas obvias en el enunciado.")

    sections.append("## Respuestas")
    sections.append(f"- Da {total} respuestas numeradas del 1 al {total}.")

    if n_vf > 0:
        sections.append(f"- Para 1..{n_vf}: SOLO `V` o `F` (sin explicación).")
    if n_short > 0:
        start = 1 if n_vf == 0 else (n_vf + 1)
        sections.append(f"- Para {start}..{total}: una sola frase corta (NO puede ser `V`/`F`).")

    fmt = "\n".join(sections)

    prompt = dedent(f"""
    Eres profesor/a. Crea un examen basado SOLO en los apuntes.

    Requisitos:
    - Total preguntas: {total}
    - Verdadero/Falso: {n_vf}
    - Respuesta corta: {n_short}
    - Reparte las preguntas entre distintos temas/secciones del texto (no te centres en un solo apartado).
    - NO uses internet ni conocimientos externos.
    - En `## Examen` SOLO van preguntas/enunciados. En `## Respuestas` SOLO van respuestas.

    Formato obligatorio (Markdown):
    {fmt}

    APUNTES:
    ---
    {apuntes_md}
    ---
    """).strip()

    return prompt


# ============================
#  Validación de salida
# ============================
def validate_output(md: str, n_vf: int, n_short: int) -> bool:
    """
    Valida la salida del modelo para detectar:
    - Que existan las secciones ## Examen y ## Respuestas
    - Que la numeración y cantidad de líneas sea coherente
    - Que V/F tenga formato "N. (V/F) ..."
    - Que respuestas V/F sean solo V o F (o Verdadero/Falso)
    - Que respuestas de corta NO sean V/F

    Esto NO valida contenido semántico (si está “bien” o “mal”),
    solo el formato para evitar outputs raros.
    """
    if "## Examen" not in md or "## Respuestas" not in md:
        return False

    total = n_vf + n_short

    # Si el usuario pidió 0 en una sección, no debería aparecer
    if n_vf == 0 and re.search(r"(?mi)^###\s+Verdadero\s+o\s+falso\b", md):
        return False
    if n_short == 0 and re.search(r"(?mi)^###\s+Respuesta\s+corta\b", md):
        return False

    exam_part, ans_part = md.split("## Respuestas", 1)

    # ---- Validar bloque V/F
    if n_vf > 0:
        m = re.search(r"(?is)###\s+Verdadero\s+o\s+falso\s*(.*?)(###\s+Respuesta\s+corta|$)", exam_part)
        if not m:
            return False
        vf_block = m.group(1).strip()

        # líneas: "1. (V/F) ..."
        vf_lines = re.findall(r"(?m)^\s*(\d+)\.\s*\(V/F\)\s+.+$", vf_block)
        if len(vf_lines) != n_vf:
            return False
        nums = sorted(int(x) for x in vf_lines)
        if nums != list(range(1, n_vf + 1)):
            return False

    # ---- Validar bloque Respuesta corta
    if n_short > 0:
        m = re.search(r"(?is)###\s+Respuesta\s+corta\s*(.*)$", exam_part)
        if not m:
            return False
        sh_block = m.group(1).strip()

        # líneas: "N. ..." pero no deben empezar con "(V/F)"
        sh_lines = re.findall(r"(?m)^\s*(\d+)\.\s+(?!\(V/F\)).+$", sh_block)
        if len(sh_lines) != n_short:
            return False

        start = 1 if n_vf == 0 else (n_vf + 1)
        nums = sorted(int(x) for x in sh_lines)
        if nums != list(range(start, total + 1)):
            return False

    # ---- Validar respuestas
    ans_lines = re.findall(r"(?m)^\s*(\d+)\.\s+(.+)$", ans_part.strip())
    if len(ans_lines) < total:
        return False

    nums_present = {int(k) for k, _ in ans_lines}
    if not all(i in nums_present for i in range(1, total + 1)):
        return False

    for k_str, content in ans_lines:
        k = int(k_str)
        c = content.strip().lower()

        # respuestas V/F
        if 1 <= k <= n_vf:
            if c not in ("v", "f", "verdadero", "falso"):
                return False

        # respuestas cortas (no pueden ser V/F)
        if n_vf < k <= total:
            if c in ("v", "f", "verdadero", "falso"):
                return False

    return True


# ============================
#  Base class: tk.Tk o tb.Window
# ============================
# Esto permite que App herede:
# - de tb.Window (si ttkbootstrap está instalado)
# - o de tk.Tk (si no lo está)
BaseWindow = tk.Tk
if TTKBOOTSTRAP_AVAILABLE:
    BaseWindow = tb.Window


# ============================
#  GUI principal
# ============================
class App(BaseWindow):
    """
    Ventana principal del programa.
    Maneja:
    - UI (inputs, checks, combos, log)
    - Hilo de trabajo (para no congelar la GUI)
    - Cancelación de streaming
    """

    def __init__(self):
        # Si hay ttkbootstrap, creamos la ventana con tema
        if TTKBOOTSTRAP_AVAILABLE:
            super().__init__(themename=DEFAULT_THEME)
        else:
            super().__init__()

        self.title("Generador de examen (PDF -> Markdown + Ollama)")
        self.geometry("900x610")

        # Cancelación para streaming
        self.cancel_event = threading.Event()

        # Hilo y cola para comunicar worker -> GUI
        self.worker_thread = None
        self.msg_queue = queue.Queue()

        # ----------------------------
        # Variables de estado (UI)
        # ----------------------------
        self.pdf_path = tk.StringVar(value="")
        self.out_dir = tk.StringVar(value=str(pathlib.Path.cwd()))
        self.host = tk.StringVar(value=DEFAULT_HOST)
        self.model = tk.StringVar(value="qwen2.5-coder:7b")
        self.num_predict = tk.StringVar(value=str(DEFAULT_NUM_PREDICT))
        self.temperature = tk.StringVar(value=str(DEFAULT_TEMPERATURE))

        self.do_archive = tk.BooleanVar(value=True)
        self.save_apuntes_md = tk.BooleanVar(value=True)

        self.use_vf = tk.BooleanVar(value=False)
        self.use_short = tk.BooleanVar(value=True)

        # StringVar para evitar TclError si borran el contenido
        self.n_vf = tk.StringVar(value="0")
        self.n_short = tk.StringVar(value="10")

        # Tema UI (solo si ttkbootstrap está instalado)
        self.ui_theme = tk.StringVar(value=DEFAULT_THEME)

        # Estado y tiempo
        self.status = tk.StringVar(value="Listo.")
        self.elapsed = tk.StringVar(value="")

        # Construir UI y eventos
        self._build_ui()
        self._wire_events()
        self._poll_queue()
        self._update_total()

    # ------------------------------------------------------
    # UI: construcción
    # ------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, **pad)

        # --- 1) PDF
        f1 = ttk.LabelFrame(frm, text="1) PDF de apuntes")
        f1.pack(fill="x", **pad)

        row = ttk.Frame(f1)
        row.pack(fill="x", padx=10, pady=8)

        ttk.Button(row, text="Seleccionar PDF...", command=self.pick_pdf).pack(side="left")
        ttk.Label(row, textvariable=self.pdf_path, wraplength=760).pack(side="left", padx=10)

        # --- 2) Salida
        f2 = ttk.LabelFrame(frm, text="2) Salida")
        f2.pack(fill="x", **pad)

        row2 = ttk.Frame(f2)
        row2.pack(fill="x", padx=10, pady=8)

        ttk.Button(row2, text="Carpeta de salida...", command=self.pick_out_dir).pack(side="left")
        ttk.Label(row2, textvariable=self.out_dir, wraplength=760).pack(side="left", padx=10)

        row2b = ttk.Frame(f2)
        row2b.pack(fill="x", padx=10, pady=4)

        ttk.Checkbutton(row2b, text="Archivar (copiar) el PDF en /archivados", variable=self.do_archive).pack(side="left")
        ttk.Checkbutton(row2b, text="Guardar también Apuntes .md", variable=self.save_apuntes_md).pack(side="left", padx=12)

        # --- 3) Ollama + Tema
        f3 = ttk.LabelFrame(frm, text="3) Ollama + UI")
        f3.pack(fill="x", **pad)

        row3 = ttk.Frame(f3)
        row3.pack(fill="x", padx=10, pady=6)

        ttk.Label(row3, text="Host:").pack(side="left")
        ttk.Entry(row3, textvariable=self.host, width=26).pack(side="left", padx=6)

        ttk.Label(row3, text="Modelo:").pack(side="left", padx=(10, 0))
        ttk.Combobox(row3, textvariable=self.model, values=MODELOS_DISPONIBLES, state="readonly", width=22).pack(side="left", padx=6)

        # Selector de tema (solo si ttkbootstrap está instalado)
        if TTKBOOTSTRAP_AVAILABLE:
            # tb.Window expone un "style" interno para themes
            ttk.Label(row3, text="Tema:").pack(side="left", padx=(12, 0))
            themes = sorted(list(self.style.theme_names()))
            cb = ttk.Combobox(row3, textvariable=self.ui_theme, values=themes, state="readonly", width=14)
            cb.pack(side="left", padx=6)
            cb.bind("<<ComboboxSelected>>", self._on_theme_change)

        row3b = ttk.Frame(f3)
        row3b.pack(fill="x", padx=10, pady=6)

        ttk.Label(row3b, text="num_predict:").pack(side="left")
        ttk.Entry(row3b, textvariable=self.num_predict, width=10).pack(side="left", padx=6)

        ttk.Label(row3b, text="temperature:").pack(side="left", padx=(10, 0))
        ttk.Entry(row3b, textvariable=self.temperature, width=10).pack(side="left", padx=6)

        # --- 4) Preguntas
        f4 = ttk.LabelFrame(frm, text=f"4) Tipos y cantidad (máximo {MAX_PREGUNTAS} en total)")
        f4.pack(fill="x", **pad)

        grid = ttk.Frame(f4)
        grid.pack(fill="x", padx=10, pady=10)

        ttk.Checkbutton(grid, text="Verdadero/Falso", variable=self.use_vf, command=self._toggle_inputs).grid(row=0, column=0, sticky="w")
        self.spin_vf = ttk.Spinbox(grid, from_=0, to=MAX_PREGUNTAS, textvariable=self.n_vf, width=6)
        self.spin_vf.grid(row=0, column=1, padx=8)

        ttk.Checkbutton(grid, text="Respuesta corta", variable=self.use_short, command=self._toggle_inputs).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.spin_short = ttk.Spinbox(grid, from_=0, to=MAX_PREGUNTAS, textvariable=self.n_short, width=6)
        self.spin_short.grid(row=1, column=1, padx=8, pady=(6, 0))

        self.lbl_total = ttk.Label(grid, text="Total: 0/10")
        self.lbl_total.grid(row=0, column=2, padx=20, rowspan=2, sticky="w")

        self._toggle_inputs()

        # --- 5) Acciones
        f5 = ttk.Frame(frm)
        f5.pack(fill="x", **pad)

        self.btn_generate = ttk.Button(f5, text="Generar examen", command=self.start_generate)
        self.btn_generate.pack(side="left")

        self.btn_cancel = ttk.Button(f5, text="Cancelar", command=self.cancel_generate, state="disabled")
        self.btn_cancel.pack(side="left", padx=8)

        # Barra indeterminada (mientras el modelo genera)
        self.progress = ttk.Progressbar(f5, mode="indeterminate", length=260)
        self.progress.pack(side="left", padx=12)

        # --- 6) Estado / Log
        f6 = ttk.LabelFrame(frm, text="Estado")
        f6.pack(fill="both", expand=True, **pad)

        ttk.Label(f6, textvariable=self.status).pack(anchor="w", padx=10, pady=(8, 2))
        ttk.Label(f6, textvariable=self.elapsed).pack(anchor="w", padx=10, pady=(0, 6))

        # Log multilinea (tk.Text) para poder insertar texto libre
        self.txt = tk.Text(f6, height=12, wrap="word")
        self.txt.pack(fill="both", expand=True, padx=10, pady=8)
        self.txt.configure(state="disabled")

    def _on_theme_change(self, _evt=None):
        """
        Aplica un tema seleccionado desde el combobox (solo si ttkbootstrap está activo).
        """
        if not TTKBOOTSTRAP_AVAILABLE:
            return
        theme = self.ui_theme.get().strip()
        if theme:
            try:
                self.style.theme_use(theme)
                self.log(f"🎨 Tema cambiado a: {theme}")
            except Exception as e:
                self.log(f"⚠️ No se pudo aplicar tema '{theme}': {e}")

    # ------------------------------------------------------
    # Eventos y validaciones de inputs
    # ------------------------------------------------------
    def _wire_events(self):
        """
        Conecta trazas para recalcular el total al escribir/cambiar checks.
        """
        self.n_vf.trace_add("write", lambda *_: self._update_total())
        self.n_short.trace_add("write", lambda *_: self._update_total())
        self.use_vf.trace_add("write", lambda *_: self._update_total())
        self.use_short.trace_add("write", lambda *_: self._update_total())

    def _toggle_inputs(self):
        """
        Habilita/deshabilita spinboxes según checks.
        """
        self.spin_vf.configure(state="normal" if self.use_vf.get() else "disabled")
        self.spin_short.configure(state="normal" if self.use_short.get() else "disabled")
        self._update_total()

    def _update_total(self):
        """
        Actualiza el label "Total" y pinta rojo si supera el máximo.
        """
        vf = safe_int(self.n_vf.get()) if self.use_vf.get() else 0
        sh = safe_int(self.n_short.get()) if self.use_short.get() else 0
        total = vf + sh

        self.lbl_total.configure(text=f"Total: {total}/{MAX_PREGUNTAS}")
        try:
            self.lbl_total.configure(foreground=("red" if total > MAX_PREGUNTAS else "black"))
        except Exception:
            # Algunos temas no respetan "foreground" de la misma forma.
            pass

    # ------------------------------------------------------
    # UI: diálogos
    # ------------------------------------------------------
    def pick_pdf(self):
        """
        Diálogo para seleccionar el PDF.
        """
        path = filedialog.askopenfilename(
            title="Selecciona un PDF",
            filetypes=[("PDF", "*.pdf")]
        )
        if path:
            self.pdf_path.set(path)

    def pick_out_dir(self):
        """
        Diálogo para seleccionar carpeta de salida.
        """
        path = filedialog.askdirectory(title="Selecciona carpeta de salida")
        if path:
            self.out_dir.set(path)

    # ------------------------------------------------------
    # Log y cola
    # ------------------------------------------------------
    def log(self, msg: str):
        """
        Inserta texto en el log (Text).
        """
        self.txt.configure(state="normal")
        self.txt.insert("end", msg + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _poll_queue(self):
        """
        Revisa la cola de mensajes (worker -> GUI) sin bloquear la interfaz.
        Esto se llama cada ~100ms con after().
        """
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "elapsed":
                    self.elapsed.set(payload)
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "error":
                    self._on_error(payload)
        except queue.Empty:
            pass

        self.after(100, self._poll_queue)

    # ------------------------------------------------------
    # Estado "ocupado" / "libre"
    # ------------------------------------------------------
    def _set_busy(self, busy: bool):
        """
        Cambia botones y barra de progreso según el estado del proceso.
        """
        self.btn_generate.configure(state="disabled" if busy else "normal")
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        if busy:
            self.progress.start(10)
        else:
            self.progress.stop()

    # ------------------------------------------------------
    # Cancelación
    # ------------------------------------------------------
    def cancel_generate(self):
        """
        Marca el evento de cancelación.
        El worker lo verá y cortará el streaming.
        """
        self.cancel_event.set()
        self.msg_queue.put(("status", "Cancelando..."))

    # ------------------------------------------------------
    # Inicio del proceso (validaciones)
    # ------------------------------------------------------
    def start_generate(self):
        """
        Valida inputs y lanza el worker en un hilo.
        """
        pdf = self.pdf_path.get().strip()
        if not pdf:
            messagebox.showwarning("Falta PDF", "Selecciona un PDF primero.")
            return

        vf = safe_int(self.n_vf.get()) if self.use_vf.get() else 0
        sh = safe_int(self.n_short.get()) if self.use_short.get() else 0
        total = vf + sh

        if total == 0:
            messagebox.showwarning("Sin preguntas", "Marca al menos un tipo y pon una cantidad.")
            return
        if total > MAX_PREGUNTAS:
            messagebox.showwarning("Límite", f"El total no puede superar {MAX_PREGUNTAS}.")
            return

        # Normalizar valores si el checkbox está apagado
        if not self.use_vf.get():
            vf = 0
            self.n_vf.set("0")
        if not self.use_short.get():
            sh = 0
            self.n_short.set("0")

        # Preparar estado
        self.cancel_event.clear()
        self._set_busy(True)
        self.msg_queue.put(("status", "Preparando..."))
        self.msg_queue.put(("elapsed", ""))

        # Worker en hilo para que la GUI no se congele
        self.worker_thread = threading.Thread(
            target=self._worker_generate,
            args=(pdf, vf, sh),
            daemon=True
        )
        self.worker_thread.start()

    # ------------------------------------------------------
    # Worker: PDF->MD + Prompt + Ollama + Guardar
    # ------------------------------------------------------
    def _worker_generate(self, pdf_path: str, n_vf: int, n_short: int):
        """
        Worker que hace el trabajo pesado fuera del hilo principal:

        1) Crea carpeta de salida si no existe
        2) (Opcional) archiva el PDF
        3) Convierte PDF -> Markdown
        4) Construye prompt
        5) Llama a Ollama (streaming) + cancelación
        6) Valida formato + reintento 1 vez si sale raro
        7) Guarda examen .md
        """
        try:
            out_dir = pathlib.Path(self.out_dir.get())
            out_dir.mkdir(parents=True, exist_ok=True)

            pdf_src = pathlib.Path(pdf_path)
            base = pdf_src.stem

            # --- Archivado del PDF (opcional)
            if self.do_archive.get():
                arch_dir = out_dir / "archivados"
                arch_dir.mkdir(parents=True, exist_ok=True)

                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                archived = arch_dir / f"{base}_{stamp}.pdf"
                shutil.copy2(pdf_src, archived)
                self.msg_queue.put(("log", f"📦 PDF archivado: {archived}"))

            # Paths de salida
            md_apuntes_path = out_dir / f"{base}_apuntes.md"
            examen_path = out_dir / f"{base}_examen.md"

            # --- PDF -> Markdown
            self.msg_queue.put(("status", "Convirtiendo PDF -> Markdown..."))
            apuntes_md = pdf_to_md(str(pdf_src), str(md_apuntes_path))
            self.msg_queue.put(("log", f"✅ Apuntes MD generado: {md_apuntes_path} ({len(apuntes_md)} chars)"))

            # Si el usuario no quiere guardar el md, lo borramos
            if not self.save_apuntes_md.get():
                try:
                    md_apuntes_path.unlink(missing_ok=True)
                    self.msg_queue.put(("log", "🧹 Apuntes MD no guardado (opción desactivada)."))
                except Exception:
                    pass

            # --- Preparar llamada a Ollama
            self.msg_queue.put(("status", "Generando examen con Ollama..."))

            model = self.model.get()
            host = self.host.get()
            num_predict = safe_int(self.num_predict.get(), DEFAULT_NUM_PREDICT)

            try:
                temperature = float((self.temperature.get() or "").strip() or DEFAULT_TEMPERATURE)
            except Exception:
                temperature = DEFAULT_TEMPERATURE

            prompt = build_prompt(apuntes_md, n_vf, n_short)

            start = time.time()

            # Callback de progreso: solo mostramos tiempo
            def on_prog(_text, elapsed):
                self.msg_queue.put(("elapsed", f"Tiempo: {elapsed:0.1f}s"))

            # --- Llamada a Ollama
            result = ollama_generate_stream(
                prompt,
                model=model,
                host=host,
                num_predict=num_predict,
                temperature=temperature,
                cancel_event=self.cancel_event,
                on_progress=on_prog
            )

            # --- Validación simple de formato (reintento 1 vez)
            if not validate_output(result, n_vf, n_short):
                self.msg_queue.put(("log", "⚠️ Salida rara. Reintento 1 vez (estricto + temp 0.0)..."))
                prompt2 = prompt + "\n\nREGLA FINAL: NO pongas respuestas en '## Examen'. Responde SOLO en '## Respuestas'. Respeta numeración 1..N."
                result = ollama_generate_stream(
                    prompt2,
                    model=model,
                    host=host,
                    num_predict=num_predict,
                    temperature=0.0,
                    cancel_event=self.cancel_event,
                    on_progress=on_prog
                )

            if not validate_output(result, n_vf, n_short):
                # Guardamos igual (modo debug) para que puedas verlo
                self.msg_queue.put(("log", "❌ Sigue raro, pero se guardó igual (debug)."))

            # --- Guardar examen
            examen_path.write_text(result.strip() + "\n", encoding="utf-8")
            self.msg_queue.put(("log", f"✅ Examen guardado: {examen_path}"))

            elapsed_total = time.time() - start
            self.msg_queue.put(("done", f"Listo. Examen generado en {elapsed_total:0.1f}s"))

        except CancelledByUser:
            # Si cancelaste, no lo tratamos como error
            self.msg_queue.put(("done", "Cancelado. No se generó el examen."))

        except Exception as e:
            # Cualquier otro error se reporta a la GUI
            self.msg_queue.put(("error", str(e)))

    # ------------------------------------------------------
    # Finalización / Error
    # ------------------------------------------------------
    def _on_done(self, msg: str):
        """
        Se llama cuando el worker termina con éxito o cancelación.
        """
        self._set_busy(False)
        self.status.set(msg)

    def _on_error(self, msg: str):
        """
        Se llama cuando el worker reporta un error.
        """
        self._set_busy(False)
        self.status.set("Error.")
        messagebox.showerror("Error", msg)


# ==========================================================
#  Entry point
# ==========================================================
if __name__ == "__main__":
    app = App()
    app.mainloop()
