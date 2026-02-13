#!/usr/bin/env python3
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
from tkinter import ttk, filedialog, messagebox

# ============================
#  CONFIG BASE
# ============================
DEFAULT_HOST = "http://localhost:11434"

MODELOS_DISPONIBLES = [
    "qwen2.5-coder:7b",
    "mistral:instruct",
    "llama3:latest",
    "deepseek-r1:latest",
]

MAX_PREGUNTAS = 10
DEFAULT_NUM_PREDICT = 950
DEFAULT_TEMPERATURE = 0.2


# ============================
#  Helpers GUI (seguro)
# ============================
def safe_int(value: str, default: int = 0) -> int:
    """
    Convierte strings a int de forma segura.
    - "" -> default
    - "e" -> default
    - "  5 " -> 5
    - "-3" -> default (no permitimos negativos)
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


# ============================
#  Ollama streaming + cancel
# ============================
class CancelledByUser(Exception):
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
        response = requests.post(url, json=payload, stream=True, timeout=(10, None))
        response.raise_for_status()

        start = time.time()

        for line in response.iter_lines(decode_unicode=True):
            if cancel_event.is_set():
                raise CancelledByUser()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            piece = data.get("response", "")
            if piece:
                chunks.append(piece)

            if on_progress:
                elapsed = time.time() - start
                on_progress("".join(chunks), elapsed)

            if data.get("done") is True:
                break

        return "".join(chunks).strip()

    except CancelledByUser:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
        raise

    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass


# ============================
#  Prompt builder (corto y sin secciones sobrantes)
# ============================
def build_prompt(apuntes_md: str, n_vf: int, n_short: int) -> str:
    total = n_vf + n_short

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
#  Validación
# ============================
def validate_output(md: str, n_vf: int, n_short: int) -> bool:
    if "## Examen" not in md or "## Respuestas" not in md:
        return False

    total = n_vf + n_short

    if n_vf == 0 and re.search(r"(?mi)^###\s+Verdadero\s+o\s+falso\b", md):
        return False
    if n_short == 0 and re.search(r"(?mi)^###\s+Respuesta\s+corta\b", md):
        return False

    exam_part, ans_part = md.split("## Respuestas", 1)

    if n_vf > 0:
        m = re.search(r"(?is)###\s+Verdadero\s+o\s+falso\s*(.*?)(###\s+Respuesta\s+corta|$)", exam_part)
        if not m:
            return False
        vf_block = m.group(1).strip()
        vf_lines = re.findall(r"(?m)^\s*(\d+)\.\s*\(V/F\)\s+.+$", vf_block)
        if len(vf_lines) != n_vf:
            return False
        nums = sorted(int(x) for x in vf_lines)
        if nums != list(range(1, n_vf + 1)):
            return False

    if n_short > 0:
        m = re.search(r"(?is)###\s+Respuesta\s+corta\s*(.*)$", exam_part)
        if not m:
            return False
        sh_block = m.group(1).strip()
        sh_lines = re.findall(r"(?m)^\s*(\d+)\.\s+(?!\(V/F\)).+$", sh_block)
        if len(sh_lines) != n_short:
            return False
        start = 1 if n_vf == 0 else (n_vf + 1)
        nums = sorted(int(x) for x in sh_lines)
        if nums != list(range(start, total + 1)):
            return False

    ans_lines = re.findall(r"(?m)^\s*(\d+)\.\s+(.+)$", ans_part.strip())
    if len(ans_lines) < total:
        return False

    nums_present = {int(k) for k, _ in ans_lines}
    if not all(i in nums_present for i in range(1, total + 1)):
        return False

    for k_str, content in ans_lines:
        k = int(k_str)
        c = content.strip().lower()

        if 1 <= k <= n_vf:
            if c not in ("v", "f", "verdadero", "falso"):
                return False

        if n_vf < k <= total:
            if c in ("v", "f", "verdadero", "falso"):
                return False

    return True


# ============================
#  GUI
# ============================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Generador de examen (PDF -> Markdown + Ollama)")
        self.geometry("880x590")

        self.cancel_event = threading.Event()
        self.worker_thread = None
        self.msg_queue = queue.Queue()

        # Estado
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

        # OJO: ahora son StringVar para evitar TclError cuando el usuario borra/escribe letras
        self.n_vf = tk.StringVar(value="0")
        self.n_short = tk.StringVar(value="10")

        self.status = tk.StringVar(value="Listo.")
        self.elapsed = tk.StringVar(value="")

        self._build_ui()
        self._wire_events()
        self._poll_queue()
        self._update_total()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, **pad)

        # --- PDF
        f1 = ttk.LabelFrame(frm, text="1) PDF de apuntes")
        f1.pack(fill="x", **pad)

        row = ttk.Frame(f1)
        row.pack(fill="x", padx=10, pady=8)

        ttk.Button(row, text="Seleccionar PDF...", command=self.pick_pdf).pack(side="left")
        ttk.Label(row, textvariable=self.pdf_path, wraplength=740).pack(side="left", padx=10)

        # --- Salida
        f2 = ttk.LabelFrame(frm, text="2) Salida")
        f2.pack(fill="x", **pad)

        row2 = ttk.Frame(f2)
        row2.pack(fill="x", padx=10, pady=8)
        ttk.Button(row2, text="Carpeta de salida...", command=self.pick_out_dir).pack(side="left")
        ttk.Label(row2, textvariable=self.out_dir, wraplength=740).pack(side="left", padx=10)

        row2b = ttk.Frame(f2)
        row2b.pack(fill="x", padx=10, pady=4)
        ttk.Checkbutton(row2b, text="Archivar (copiar) el PDF en /archivados", variable=self.do_archive).pack(side="left")
        ttk.Checkbutton(row2b, text="Guardar también Apuntes .md", variable=self.save_apuntes_md).pack(side="left", padx=12)

        # --- Ollama
        f3 = ttk.LabelFrame(frm, text="3) Ollama")
        f3.pack(fill="x", **pad)

        row3 = ttk.Frame(f3)
        row3.pack(fill="x", padx=10, pady=6)
        ttk.Label(row3, text="Host:").pack(side="left")
        ttk.Entry(row3, textvariable=self.host, width=26).pack(side="left", padx=6)
        ttk.Label(row3, text="Modelo:").pack(side="left", padx=(10, 0))
        ttk.Combobox(row3, textvariable=self.model, values=MODELOS_DISPONIBLES, state="readonly", width=22).pack(side="left", padx=6)

        row3b = ttk.Frame(f3)
        row3b.pack(fill="x", padx=10, pady=6)
        ttk.Label(row3b, text="num_predict:").pack(side="left")
        ttk.Entry(row3b, textvariable=self.num_predict, width=10).pack(side="left", padx=6)
        ttk.Label(row3b, text="temperature:").pack(side="left", padx=(10, 0))
        ttk.Entry(row3b, textvariable=self.temperature, width=10).pack(side="left", padx=6)

        # --- Preguntas
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

        # --- Acciones
        f5 = ttk.Frame(frm)
        f5.pack(fill="x", **pad)

        self.btn_generate = ttk.Button(f5, text="Generar examen", command=self.start_generate)
        self.btn_generate.pack(side="left")

        self.btn_cancel = ttk.Button(f5, text="Cancelar", command=self.cancel_generate, state="disabled")
        self.btn_cancel.pack(side="left", padx=8)

        self.progress = ttk.Progressbar(f5, mode="indeterminate", length=250)
        self.progress.pack(side="left", padx=12)

        # --- Estado / Log
        f6 = ttk.LabelFrame(frm, text="Estado")
        f6.pack(fill="both", expand=True, **pad)

        ttk.Label(f6, textvariable=self.status).pack(anchor="w", padx=10, pady=(8, 2))
        ttk.Label(f6, textvariable=self.elapsed).pack(anchor="w", padx=10, pady=(0, 6))

        self.txt = tk.Text(f6, height=12, wrap="word")
        self.txt.pack(fill="both", expand=True, padx=10, pady=8)
        self.txt.configure(state="disabled")

    def _wire_events(self):
        # Con StringVar, esto ya no peta aunque esté vacío
        self.n_vf.trace_add("write", lambda *_: self._update_total())
        self.n_short.trace_add("write", lambda *_: self._update_total())
        self.use_vf.trace_add("write", lambda *_: self._update_total())
        self.use_short.trace_add("write", lambda *_: self._update_total())

    def _toggle_inputs(self):
        self.spin_vf.configure(state="normal" if self.use_vf.get() else "disabled")
        self.spin_short.configure(state="normal" if self.use_short.get() else "disabled")
        self._update_total()

    def _update_total(self):
        vf = safe_int(self.n_vf.get()) if self.use_vf.get() else 0
        sh = safe_int(self.n_short.get()) if self.use_short.get() else 0
        total = vf + sh

        self.lbl_total.configure(text=f"Total: {total}/{MAX_PREGUNTAS}")
        self.lbl_total.configure(foreground=("red" if total > MAX_PREGUNTAS else "black"))

    def pick_pdf(self):
        path = filedialog.askopenfilename(
            title="Selecciona un PDF",
            filetypes=[("PDF", "*.pdf")]
        )
        if path:
            self.pdf_path.set(path)

    def pick_out_dir(self):
        path = filedialog.askdirectory(title="Selecciona carpeta de salida")
        if path:
            self.out_dir.set(path)

    def log(self, msg: str):
        self.txt.configure(state="normal")
        self.txt.insert("end", msg + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _poll_queue(self):
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

    def _set_busy(self, busy: bool):
        self.btn_generate.configure(state="disabled" if busy else "normal")
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        if busy:
            self.progress.start(10)
        else:
            self.progress.stop()

    def cancel_generate(self):
        self.cancel_event.set()
        self.msg_queue.put(("status", "Cancelando..."))

    def start_generate(self):
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

        # Normalizar valores (por si estaban raros)
        if not self.use_vf.get():
            vf = 0
            self.n_vf.set("0")
        if not self.use_short.get():
            sh = 0
            self.n_short.set("0")

        self.cancel_event.clear()
        self._set_busy(True)
        self.msg_queue.put(("status", "Preparando..."))
        self.msg_queue.put(("elapsed", ""))

        self.worker_thread = threading.Thread(
            target=self._worker_generate,
            args=(pdf, vf, sh),
            daemon=True
        )
        self.worker_thread.start()

    def _worker_generate(self, pdf_path: str, n_vf: int, n_short: int):
        try:
            out_dir = pathlib.Path(self.out_dir.get())
            out_dir.mkdir(parents=True, exist_ok=True)

            pdf_src = pathlib.Path(pdf_path)
            base = pdf_src.stem

            if self.do_archive.get():
                arch_dir = out_dir / "archivados"
                arch_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                archived = arch_dir / f"{base}_{stamp}.pdf"
                shutil.copy2(pdf_src, archived)
                self.msg_queue.put(("log", f"📦 PDF archivado: {archived}"))

            md_apuntes_path = out_dir / f"{base}_apuntes.md"
            examen_path = out_dir / f"{base}_examen.md"

            self.msg_queue.put(("status", "Convirtiendo PDF -> Markdown..."))
            apuntes_md = pdf_to_md(str(pdf_src), str(md_apuntes_path))
            self.msg_queue.put(("log", f"✅ Apuntes MD generado: {md_apuntes_path} ({len(apuntes_md)} chars)"))

            if not self.save_apuntes_md.get():
                try:
                    md_apuntes_path.unlink(missing_ok=True)
                    self.msg_queue.put(("log", "🧹 Apuntes MD no guardado (opción desactivada)."))
                except Exception:
                    pass

            self.msg_queue.put(("status", "Generando examen con Ollama..."))
            model = self.model.get()
            host = self.host.get()

            num_predict = safe_int(self.num_predict.get(), DEFAULT_NUM_PREDICT)
            temperature = float(self.temperature.get().strip() or DEFAULT_TEMPERATURE)

            prompt = build_prompt(apuntes_md, n_vf, n_short)

            start = time.time()

            def on_prog(_text, elapsed):
                self.msg_queue.put(("elapsed", f"Tiempo: {elapsed:0.1f}s"))

            result = ollama_generate_stream(
                prompt,
                model=model,
                host=host,
                num_predict=num_predict,
                temperature=temperature,
                cancel_event=self.cancel_event,
                on_progress=on_prog
            )

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
                self.msg_queue.put(("log", "❌ Sigue raro, pero se guardó igual (debug)."))

            examen_path.write_text(result.strip() + "\n", encoding="utf-8")
            self.msg_queue.put(("log", f"✅ Examen guardado: {examen_path}"))

            elapsed_total = time.time() - start
            self.msg_queue.put(("done", f"Listo. Examen generado en {elapsed_total:0.1f}s"))

        except CancelledByUser:
            self.msg_queue.put(("done", "Cancelado. No se generó el examen."))
        except Exception as e:
            self.msg_queue.put(("error", str(e)))

    def _on_done(self, msg: str):
        self._set_busy(False)
        self.status.set(msg)

    def _on_error(self, msg: str):
        self._set_busy(False)
        self.status.set("Error.")
        messagebox.showerror("Error", msg)


if __name__ == "__main__":
    app = App()
    app.mainloop()
