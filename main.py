import os
import time
import uuid
import threading
import inspect
import base64
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import requests

# Optional AI + document libs
try:
    import openai
except Exception:
    openai = None

try:
    from docx import Document
except Exception:
    Document = None

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
except Exception:
    canvas = None

# ---------- Config (env vars) ----------
MP_ACCESS_TOKEN = (os.getenv("MP_ACCESS_TOKEN") or "").strip()  # token Mercado Pago (TEST-... or prod)
BASE_URL = os.getenv("BASE_URL") or ""  # e.g. "https://mi-proyecto.up.railway.app"
PORT = int(os.getenv("PORT", 8000))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # opcional, para generación de texto con IA
ENABLE_SIMULATE = os.getenv("ENABLE_SIMULATE", "0") == "1"  # habilita /simulate-paid si "1"

if not MP_ACCESS_TOKEN:
    raise RuntimeError("Por favor configura MP_ACCESS_TOKEN en variables de entorno")

if not BASE_URL:
    # No se frena, pero es recomendable configurar BASE_URL en Railway.
    BASE_URL = "https://TU_DOMINIO_RAILWAY"

if OPENAI_API_KEY and openai:
    openai.api_key = OPENAI_API_KEY

# ---------- App ----------
app = FastAPI(title="RedaXion - Backend mínimo (actualizado)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir archivos generados desde /tmp vía /files
FILES_PATH = "/tmp"
app.mount("/files", StaticFiles(directory=FILES_PATH), name="files")

# ---------- In-memory store ----------
ORDERS = {}  # order_id -> {email, payload, status, created_at, payment_id, access_code, pdf_url, docx_url}

def generate_access_code():
    t = time.strftime("%y%m%d%H%M%S")
    random = uuid.uuid4().hex[:4].upper()
    return f"RX-{t}-{random}"

# ---------- Helper: call generate_and_deliver with correct args ----------
def _run_generate_with_correct_args(order_id: str, payer_email: Optional[str]):
    """
    Detecta la firma de generate_and_deliver y la invoca con 0/1/2 args según corresponda.
    """
    try:
        fn = generate_and_deliver  # noqa: F821
    except NameError:
        print(f"[simulate] generate_and_deliver no definida. Simulación para {order_id}")
        time.sleep(1)
        print(f"[simulate] Simulación completada para {order_id}")
        return

    try:
        sig = inspect.signature(fn)
        n_params = len(sig.parameters)
        print(f"[simulate] generate_and_deliver encontrada con {n_params} param(s). Llamando apropiadamente.")
        if n_params == 0:
            fn()
        elif n_params == 1:
            fn(order_id)
        else:
            fn(order_id, payer_email)
    except Exception as e:
        print(f"[simulate] Error al ejecutar generate_and_deliver dinámicamente: {e}")

# ---------- Endpoints ----------

@app.post("/create-preference")
async def create_preference(req: Request):
    """
    Wix llamará a este endpoint con JSON:
    { order_id, email, subject, topic, bloom, mcqCount, essayCount, includeSolutions }
    Devuelve: { init_point, preference_id, order_id }
    """
    data = await req.json()
    order_id = data.get("order_id") or f"ORD-{uuid.uuid4().hex[:8].upper()}"
    email = data.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Falta email")

    # Guardamos el pedido en memoria
    ORDERS[order_id] = {
        "email": email,
        "payload": data,
        "status": "pending",
        "created_at": time.time()
    }

    # Crear preference en Mercado Pago
    preference_payload = {
        "items": [
            {"title": "RedaXion - Generador de examen", "quantity": 1, "unit_price": 2000.0}
        ],
        "external_reference": order_id,
        "payer": {"email": email},
        "notification_url": f"{BASE_URL}/mp-webhook",
        "back_urls": {
            "success": f"{BASE_URL}/mp-success?order_id={order_id}",
            "failure": f"{BASE_URL}/mp-failure?order_id={order_id}"
        },
        "auto_return": "approved"
    }

    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    resp = requests.post("https://api.mercadopago.com/checkout/preferences", json=preference_payload, headers=headers)
    if resp.status_code not in (200, 201):
        return {"error": "No se pudo crear preference", "details": resp.text}

    pref = resp.json()
    init_point = pref.get("init_point") or pref.get("sandbox_init_point")
    preference_id = pref.get("id")

    ORDERS[order_id]["preference_id"] = preference_id

    return {"init_point": init_point, "preference_id": preference_id, "order_id": order_id}


@app.post("/mp-webhook")
async def mp_webhook(req: Request):
    """
    Mercado Pago notificará aquí (webhook). Validamos la transacción con la API de MP.
    """
    try:
        body = await req.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    # Extraer payment id (MP envía varias formas)
    payment_id = None
    if isinstance(body, dict):
        if "data" in body and isinstance(body["data"], dict) and "id" in body["data"]:
            payment_id = body["data"]["id"]
        elif "id" in body:
            payment_id = body["id"]

    if not payment_id:
        # No hay payment id: devolver 200 para que MP no vuelva a reenviar
        return {"ok": True, "note": "no payment id"}

    # Validar pago con la API de Mercado Pago
    r = requests.get(f"https://api.mercadopago.com/v1/payments/{payment_id}",
                     headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"})
    if r.status_code != 200:
        # Registrar y responder 200 para evitar reintentos infinitos; el cuerpo incluye detalles
        print(f"[mp-webhook] validation failed for payment {payment_id}: {r.status_code} {r.text}")
        return {"error": "No se pudo validar pago", "details": r.text}

    payment = r.json()
    status = payment.get("status")
    if status != "approved":
        print(f"[mp-webhook] payment {payment_id} status: {status}")
        return {"ok": True, "status": status}

    external_ref = payment.get("external_reference") or (payment.get("metadata") or {}).get("order_id")
    payer_email = payment.get("payer", {}).get("email")
    amount = payment.get("transaction_amount")

    if not external_ref:
        return {"ok": False, "error": "external_reference missing"}

    # Obtener o crear order
    order = ORDERS.get(external_ref)
    if not order:
        ORDERS[external_ref] = {"email": payer_email, "payload": {}, "status": "paid", "created_at": time.time()}
        order = ORDERS[external_ref]

    order["status"] = "paid"
    order["payment_id"] = payment_id
    order["amount"] = amount
    order["payer_email"] = payer_email

    # Generar código y guardar
    code = generate_access_code()
    order["access_code"] = code

    # Lanzar generación del examen en background (invoca detectando firma)
    threading.Thread(target=_run_generate_with_correct_args, args=(external_ref, payer_email), daemon=True).start()

    return {"ok": True, "order": external_ref, "code": code}


# ---------- Document generation helpers ----------
def generate_text_with_openai(subject: str, topic: str, mcq_count: int = 14, essay_count: int = 2) -> str:
    """
    Prompt maestro: genera TCP + RedaQuiz con formato enriquecido (markdown-like)
    y cuidado estético. Está pensado para producir texto que luego se convierta
    a DOCX/PDF con encabezados, negritas e itálicas.
    """
    PROMPT = f"""
Eres RedaXion, un sistema experto en generación de material académico. Tu salida debe estar en ESPAÑOL y cumplir exactamente con las siguientes reglas y formato.

OBJETIVO:
- Producir una Transcripción Académica Profesional (TCP) + un RedaQuiz (preguntas de alternativa y desarrollo).
- Basar las afirmaciones clínicas/teóricas en la **mejor evidencia académica disponible**: cuando menciones recomendaciones o afirmaciones, indica la fuerza de la evidencia entre paréntesis: (evidencia fuerte / evidencia moderada / evidencia limitada). No pongas URLs.

ESTÉTICA Y MARCADO:
- Usa un marcado sencillo tipo Markdown para que el documento final quede estético:
  - Títulos principales: `# Título`
  - Subtítulos: `## Subtítulo`
  - Negrita: `**texto en negrita**`
  - Itálica: `*texto en itálica*`
  - Listas con `- ` o `1. `
- Al principio del documento, en el encabezado visible en la primera página (header), incluye exactamente, en **negrita e itálica**:
  `RedaXion, tecnología que transforma tu estudio`
  (en el texto de salida, incluye una línea separada marcada así: `<<HEADER: RedaXion, tecnología que transforma tu estudio>>` — el procesador de DOCX deberá convertirla al header).

ESTRUCTURA DEL DOCUMENTO (orden obligatorio):
1) `# TCP`
   - `## Introducción` — breve, contextualiza el tema.
   - `## Conceptos clave` — bullets con definiciones cortas.
   - `## Marco teórico y evidencia` — resumen con referencia a la fuerza de la evidencia entre paréntesis.
   - `## Aplicaciones/práctica` — ejemplos, analogías o caso clínico breve si aplica.
   - `## Perlas` — 3–5 viñetas con conceptos para recordar.

2) `# RedaQuiz`
   - `## Preguntas de alternativa` — EXACTAMENTE {mcq_count} preguntas numeradas:
     - Formato por pregunta (texto plano con marcado ligero si hace falta):
       ```
       1) Enunciado de la pregunta
       A. Opción A
       B. Opción B
       C. Opción C
       D. Opción D
       E. Opción E
       ```
     - Cada pregunta debe ser de nivel universitario (evaluación aplicada), con distractores plausibles.

   - `## Preguntas de desarrollo` — EXACTAMENTE {essay_count} preguntas:
     - Cada enunciado debe incluir contexto y pedir una respuesta apoyada en evidencia; pedir explícitamente criterios a considerar (ej.: "incluya: 1) diagnóstico diferencial, 2) pruebas pertinentes, 3) manejo inicial y 4) evidencia que sustente la decisión").

3) Después del bloque de preguntas inserta **15 líneas en blanco** (esto es sagrado).

4) `# Solucionario`
   - Primero, solucionario para las preguntas de alternativa: para cada número, escribe la LETRA CORRECTA en formato:
     `1) B — Justificación breve (1–2 líneas).` Indicar por qué los distractores son incorrectos (1 línea).
   - Luego, guía de corrección para preguntas de desarrollo: por cada pregunta, 3–5 viñetas con los puntos que debe contener la respuesta (incluye referencias a la fuerza de la evidencia entre paréntesis).

OTRAS INSTRUCCIONES:
- Mantén el lenguaje técnico pero claro.
- Evita frases vagas; cuando hagas afirmaciones sobre tratamientos/diagnósticos, indica la fuerza de la evidencia entre paréntesis.
- No incluyas URLs ni citas bibliográficas largas; indicaciones breves sobre la evidencia bastan.
- La salida final debe ser un bloque de texto único (con el marcado descrito), listo para pasarse a DOCX/PDF.
- Evita listas excesivamente largas en las preguntas; cada pregunta debe ocupar 4–8 líneas en el enunciado + las opciones.

PARÁMETROS:
- Materia: {subject}
- Tema: {topic}
- MCQ: {mcq_count}
- Desarrollo: {essay_count}

Genera el documento ahora, siguiendo estrictamente el formato y la estética solicitada.
"""
    # Llamada a OpenAI (si está disponible)
    if OPENAI_API_KEY and openai:
        try:
            print("[generate_text_with_openai] Llamando a OpenAI con Prompt Maestro estético y basado en evidencia...")
            resp = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": PROMPT}],
                temperature=0.15,
                max_tokens=3500
            )
            text = resp["choices"][0]["message"]["content"]
            print(f"[generate_text_with_openai] OpenAI OK — longitud: {len(text)} chars")
            return text
        except Exception as e:
            print(f"[generate_text_with_openai] OpenAI error: {e}")

    # Fallback si OpenAI no responde
    fallback = [
        f"Tema: {topic}",
        "",
        "Introducción: (fallback) texto breve.",
        "",
        f"Preguntas de alternativa (ejemplo): se generarán {mcq_count} preguntas.",
        "",
        "Preguntas de desarrollo (ejemplo):",
    ]
    for i in range(1, essay_count + 1):
        fallback.append(f"{i}. Desarrollo {i}: Respuesta con evidencia (fallback).")
    fallback.append("")
    fallback.append("Solucionario: (respuestas de ejemplo)")
    return "\n".join(fallback)

from docx import Document
from docx.shared import Pt
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT

def save_docx_from_text(text: str, path: str) -> None:
    """
    Interpreta un marcado sencillo (basado en lo pedido en el Prompt Maestro)
    y crea un DOCX con:
     - Header con: RedaXion, tecnología que transforma tu estudio (negrita + itálica)
     - Headings (#, ##), negrita ** ** e itálica * *
     - Listas básicas y párrafos
    """
    if Document is None:
        raise RuntimeError("python-docx no instalado")

    doc = Document()
    # HEADER: buscar token especial si el prompt lo incluyó
    # Buscamos la línea marcador: <<HEADER: ... >>
    header_text = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("<<HEADER:") and line.endswith(">>"):
            header_text = line.replace("<<HEADER:", "").rstrip(">>").strip()
            break

    if header_text:
        section = doc.sections[0]
        header = section.header
        ph = header.paragraphs[0]
        run = ph.add_run(header_text)
        run.bold = True
        run.italic = True
        ph.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        ph.style = doc.styles['Normal']
        # remover la línea del body (no queremos que aparezca dos veces)
        text = text.replace(f"<<HEADER: {header_text}>>", "")

    # Parse simple markdown-like
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if not line:
            doc.add_paragraph("")  # blank line
            continue

        # Heading level 1: "# "
        if line.startswith("# "):
            p = doc.add_heading(line[2:].strip(), level=1)
            continue
        # Heading level 2: "## "
        if line.startswith("## "):
            p = doc.add_heading(line[3:].strip(), level=2)
            continue

        # Bulleted list (start with "- ")
        if line.startswith("- "):
            p = doc.add_paragraph(line[2:].strip(), style='List Bullet')
            continue

        # Numbered list "1. "
        if line[:3].strip().isdigit() and line[2] == '.':
            # crude check for "1. "
            p = doc.add_paragraph(line, style='List Number')
            continue

        # Inline bold **text** and italic *text* handling
        p = doc.add_paragraph()
        i = 0
        while i < len(line):
            if line[i:i+2] == "**":
                # bold until next **
                j = line.find("**", i+2)
                if j == -1:
                    run = p.add_run(line[i:])
                    break
                run = p.add_run(line[i+2:j])
                run.bold = True
                i = j+2
            elif line[i] == "*" and (i+1 < len(line) and line[i+1] != " "):
                # italic until next *
                j = line.find("*", i+1)
                if j == -1:
                    run = p.add_run(line[i:])
                    break
                run = p.add_run(line[i+1:j])
                run.italic = True
                i = j+1
            else:
                # normal text until next special
                # find next special
                nxt = line.find("**", i)
                ni = line.find("*", i)
                if nxt == -1 and ni == -1:
                    run = p.add_run(line[i:])
                    break
                # choose nearest
                if nxt == -1:
                    nextpos = ni
                elif ni == -1:
                    nextpos = nxt
                else:
                    nextpos = min(nxt, ni)
                run = p.add_run(line[i:nextpos])
                i = nextpos

    # Small global styling: set default font size for all paragraphs (improve legibility)
    style = doc.styles['Normal']
    font = style.font
    font.name = 'Calibri'
    font.size = Pt(11)

    doc.save(path)



def save_pdf_from_text(text: str, path: str) -> None:
    if canvas is None:
        raise RuntimeError("reportlab no instalado")
    c = canvas.Canvas(path, pagesize=letter)
    width, height = letter
    margin = 40
    y = height - margin
    # simple wrap
    for paragraph in text.split("\n\n"):
        for l in paragraph.split("\n"):
            l = l.strip()
            if not l:
                y -= 10
                continue
            # naive wrapping at 90 chars
            while len(l) > 90:
                if y < margin + 20:
                    c.showPage()
                    y = height - margin
                c.drawString(margin, y, l[:90])
                l = l[90:]
                y -= 12
            if y < margin + 20:
                c.showPage()
                y = height - margin
            c.drawString(margin, y, l)
            y -= 12
        y -= 6
    c.save()


# ---------- Core: generate and deliver ----------
# --- Reemplaza la función generate_and_deliver por esta versión mejorada ---
def generate_and_deliver(order_id: str, payer_email: Optional[str] = None):
    """
    Genera el examen, guarda DOCX/PDF en /tmp y actualiza ORDERS con los links.
    Esta versión añade logging extra para depuración.
    """
    print(f"[generate_and_deliver] START order {order_id} for {payer_email}")
    order = ORDERS.get(order_id, {})
    payload = order.get("payload", {}) if isinstance(order, dict) else {}
    subject = payload.get("subject", "Medicina")
    topic = payload.get("topic", "Tema de prueba")
    mcq_count = int(payload.get("mcqCount", payload.get("mcq_count", 14) or 14))
    essay_count = int(payload.get("essayCount", payload.get("essay_count", 2) or 2))

    # marcar processing
    order["status"] = "processing"
    ORDERS[order_id] = order

    # Chequeo de librerías disponibles
    have_openai = (openai is not None) and bool(OPENAI_API_KEY)
    have_docx = Document is not None
    have_pdf_lib = canvas is not None
    print(f"[generate_and_deliver] libs -> openai:{have_openai} python-docx:{have_docx} reportlab:{have_pdf_lib}")

    # 1) generar texto (OpenAI si disponible)
    try:
        exam_text = generate_text_with_openai(subject, topic, mcq_count, essay_count)
        print(f"[generate_and_deliver] Generated exam text length: {len(exam_text) if exam_text else 0}")
    except Exception as e:
        exam_text = f"Error generando texto: {e}\n\nSe generó texto fallback."
        print(f"[generate_and_deliver] Error al generar texto: {e}")

    # 2) guardar DOCX y PDF en /tmp (FILES_PATH)
    safe_order = order_id.replace("/", "_")
    docx_path = os.path.join(FILES_PATH, f"{safe_order}.docx")
    pdf_path = os.path.join(FILES_PATH, f"{safe_order}.pdf")

    # Guardar DOCX (solo si python-docx presente)
    if have_docx:
        try:
            save_docx_from_text(exam_text, docx_path)
            print(f"[generate_and_deliver] DOCX saved: {docx_path}")
        except Exception as e:
            print(f"[generate_and_deliver] Error saving DOCX: {e}")
    else:
        # Crear fallback .docx-free: guardar .txt con nombre .docx para ver contenido
        try:
            txt_path = os.path.join(FILES_PATH, f"{safe_order}.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(exam_text)
            # también crear una copia con extensión .docx para que se vea algo
            with open(docx_path, "w", encoding="utf-8") as f2:
                f2.write(exam_text)
            print(f"[generate_and_deliver] python-docx no instalado: se escribió fallback TXT+DOCX at {txt_path} and {docx_path}")
        except Exception as e:
            print(f"[generate_and_deliver] Error fallback saving TXT/DOCX: {e}")

    # Guardar PDF (solo si reportlab presente)
    if have_pdf_lib:
        try:
            save_pdf_from_text(exam_text, pdf_path)
            print(f"[generate_and_deliver] PDF saved: {pdf_path}")
        except Exception as e:
            print(f"[generate_and_deliver] Error saving PDF: {e}")
    else:
        # Fallback: crear un archivo .txt y también copiarlo con extensión .pdf (no es PDF real,
        # pero servirá para pruebas. Indicará que falta reportlab.)
        try:
            fallback_txt = os.path.join(FILES_PATH, f"{safe_order}_fallback.txt")
            with open(fallback_txt, "w", encoding="utf-8") as f:
                f.write("FALLBACK PDF (reportlab no instalado).\n\n" + exam_text)
            # create a copy named .pdf so StaticFiles can serve something
            with open(pdf_path, "w", encoding="utf-8") as f2:
                f2.write("FALLBACK PDF (reportlab no instalado).\n\n" + exam_text)
            print(f"[generate_and_deliver] reportlab no instalado: se escribió fallback TXT y archivo con extensión .pdf at {fallback_txt} and {pdf_path}")
        except Exception as e:
            print(f"[generate_and_deliver] Error fallback saving PDF: {e}")

    # 3) actualizar ORDERS con URLs públicas (se sirven via /files)
    base_for_links = BASE_URL if str(BASE_URL).startswith("http") else f"https://{BASE_URL}"
    pdf_url = f"{base_for_links}/files/{os.path.basename(pdf_path)}"
    docx_url = f"{base_for_links}/files/{os.path.basename(docx_path)}"

    order["pdf_url"] = pdf_url
    order["docx_url"] = docx_url
    order["status"] = "ready"
    order["delivered_at"] = time.time()
    ORDERS[order_id] = order

    print(f"[generate_and_deliver] DONE order {order_id}. Links: {pdf_url} {docx_url}")

@app.get("/order-status")
async def order_status(order_id: str):
    """Wix puede consultar este endpoint para saber si el pedido está listo."""
    order = ORDERS.get(order_id)
    if not order:
        return {"status": "not_found"}
    return {
        "status": order.get("status"),
        "pdf_url": order.get("pdf_url"),
        "docx_url": order.get("docx_url"),
        "access_code": order.get("access_code"),
        "payment_id": order.get("payment_id"),
    }

# ---------- Optional simulate endpoint (only if ENABLE_SIMULATE=1) ----------
if ENABLE_SIMULATE:
    @app.post("/simulate-paid")
    async def simulate_paid(request: Request):
        payload = await request.json()
        order_id = payload.get("order_id") or f"SIM-{int(time.time())}"
        payer_email = payload.get("payer_email") or "cliente@ejemplo.com"

        # asegurar order en memoria
        ORDERS.setdefault(order_id, {})
        ORDERS[order_id]["status"] = "paid"
        ORDERS[order_id]["payer_email"] = payer_email
        ORDERS[order_id]["payment_id"] = f"SIM-{uuid.uuid4().hex[:8]}"
        ORDERS[order_id]["access_code"] = generate_access_code()

        # lanzar generación en background
        threading.Thread(target=_run_generate_with_correct_args, args=(order_id, payer_email), daemon=True).start()

        return {"ok": True, "simulated": True, "order": order_id, "payer_email": payer_email}

# --- Endpoint temporal para ver qué archivos hay en /tmp ---
import os
@app.get("/debug-list-files")
async def debug_list_files():
    try:
        files = os.listdir(FILES_PATH)
        ord_files = [f for f in files if f.startswith("ORD-")]
        return {"ok": True, "FILES_PATH": FILES_PATH, "count": len(ord_files), "files": ord_files}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------- End of file ----------
