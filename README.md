# Ollama Test Generator (PDF → Markdown → Examen) — `ollama_test_gen.py`

Generador de exámenes con **IA local (Ollama)** a partir de **apuntes en PDF**, con **interfaz gráfica** (Tkinter) y soporte de **temas** opcional con `ttkbootstrap`.

> Pensado para apuntes de cualquier asignatura (IPE, marketing, etc.).  
> Funciona 100% local: tu PDF nunca sale de tu PC.

---

## ✨ Qué hace

1. Seleccionas un **PDF** con apuntes.
2. El script convierte el PDF a **Markdown** (simple).
3. Envía el Markdown a **Ollama** para generar un examen con:
   - **Verdadero/Falso**
   - **Respuesta corta**
4. Guarda el resultado en un **.md** en la carpeta de salida.
5. Opcionalmente:
   - Archiva el PDF original en `/archivados`
   - Guarda también el `.md` de apuntes

Incluye:
- **Cancelación** (botón “Cancelar” corta el streaming de Ollama).
- Validación de formato + **1 reintento** si la salida viene rara.
- Selector de **modelo** y parámetros (`num_predict`, `temperature`).
- UI con temas si instalas `ttkbootstrap`.

---

## ✅ Requisitos

- Python **3.10+** (recomendado)
- Ollama instalado y corriendo localmente
- Modelos descargados en Ollama (ej: `qwen2.5-coder:7b`)

---

## 📦 Instalación

### 1) Clona el repo
```bash
git clone <TU_REPO>
cd <TU_REPO>
````

### 2) Instala dependencias

```bash
py -m pip install requests pypdf
```

### 3) (Opcional) Temas bonitos para la interfaz

```bash
py -m pip install ttkbootstrap
```

> Si **no** instalas `ttkbootstrap`, la app funciona igual con Tkinter normal.

---

## ▶️ Uso

Arranca la interfaz:

```bash
py ollama_test_gen.py
```

En la GUI:

1. **Selecciona PDF**
2. Elige **carpeta de salida**
3. Selecciona **modelo** y ajusta `num_predict` / `temperature`
4. Marca tipos de preguntas y cantidades (máximo 10 en total)
5. Click en **Generar examen**

### Salidas generadas

En tu carpeta de salida se crea:

* `NOMBREPDF_examen.md` ✅ (siempre)
* `NOMBREPDF_apuntes.md` ✅ (opcional)
* `archivados/NOMBREPDF_YYYYmmdd_HHMMSS.pdf` ✅ (opcional)

---

## 🧠 Modelos recomendados (Ollama)

Ejemplos de modelos que puedes probar (si los tienes descargados):

* `qwen2.5-coder:7b` (rápido, suele seguir bien el formato)
* `mistral:instruct` (buena redacción, a veces más “creativo”)
* `llama3:latest`
* `deepseek-r1:latest` (razona bien pero puede ser más lento)

---

## 🎛️ Parámetros importantes

* **num_predict**: límite aproximado de tokens de salida.
  Si la respuesta se corta, súbelo (ej: 950 → 1200).
* **temperature**: creatividad.
  Para seguir formato y no “inventar”, suele ir bien 0.0–0.3.

---

## ⚠️ Notas y problemas comunes

### 1) “El examen sale raro” o con formato incorrecto

* Baja `temperature` (ej: 0.2 → 0.0)
* Sube `num_predict`
* Prueba otro modelo

El script hace **1 reintento automático** si detecta formato inválido.

### 2) PDFs escaneados (sin texto seleccionable)

Si el PDF es una imagen escaneada, `pypdf` puede extraer poco o nada.
Solución: usa un PDF con texto real (o pásalo por OCR antes).

### 3) Cancelar tarda en reaccionar

Depende del modelo, pero el script corta el stream al detectar cancelación.

---

## 🎨 Temas (ttkbootstrap)

Si instalaste `ttkbootstrap`, puedes cambiar el tema desde la GUI.

Temas recomendados:

* Dark: `superhero`, `darkly`, `cyborg`, `solar`, `vapor`
* Light: `flatly`, `litera`, `minty`, `cosmo`, `pulse`

---

##  Autor

Hecho por **Piero Olivares**
