import os
import time
import uuid
import threading
import inspect
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import requests

# Optional AI + document libs (con fallbacks)
try:
    import openai
except Exception:
    openai = None

try:
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
except Exception:
    Document = None
    Pt = None
    RGBColor = None
    WD_PARAGRAPH_ALIGNMENT = None

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    # import simpleSplit later inside function to avoid import error if not present
except Exception:
    canvas = None
    letter = None

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
    # Si openai está presente, asigna la clave
    try:
        openai.api_key = OPENAI_API_KEY
    except Exception:
        # Algunas versiones nuevas usan otra inicialización; pero mantenemos esto soportado
        pass

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
ORDERS = {}  # order_id -> {email, payload, status, created_at, payment_id, access_code, exam_pdf_url, ...}

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
    Prompt maestro: genera DOS bloques separados por la línea exacta:
    <<SOLUTIONS>>
    Bloque A (examen): solo texto en negro, minimalista.
    Bloque B (solucionario): sólo respuestas y guías de corrección.
    """
    PROMPT = f"""
Eres RedaXion. Produce DOS BLOQUES separados exactamente por la línea:
<<SOLUTIONS>>

BLOQUE A — EXAMEN (solo esto debe contener el primer bloque):
Formato requerido (texto plano, todo en negro, sin adornos):
Asignatura: {subject}
Tema: {topic}

Sección "Preguntas de alternativa":
Genera EXACTAMENTE {mcq_count} preguntas numeradas (1) a ({mcq_count}) con 5 opciones A–E cada una.
Formato por pregunta:
1) Enunciado
A. Opción A
B. Opción B
C. Opción C
D. Opción D
E. Opción E

Sección "Preguntas de desarrollo":
Genera EXACTAMENTE {essay_count} preguntas numeradas (p. ej. 1) ...) que pidan respuestas basadas en evidencia (indica en el enunciado: "Responda apoyándose en la mejor evidencia disponible y mencione criterios a evaluar").

IMPORTANTE:
- No incluyas el solucionario en este bloque.
- Todo en texto plano, sin colores, sin encabezados extras, sin logos, sin explicaciones adicionales.

Luego escribe la línea:
<<SOLUTIONS>>

BLOQUE B — SOLUCIONARIO (solo esto debe contener el segundo bloque):
- Para cada pregunta de alternativa, indica: `1) B — Justificación breve (1–2 líneas).`
- Para cada pregunta de desarrollo, entrega una guía de corrección en 3–5 viñetas (puntos clave que debe contener la respuesta). No más texto.

No incluyas nada fuera de estos dos bloques. Salida en español.
"""
    # llamada a OpenAI (ya adaptada a tu versión actual)
    if OPENAI_API_KEY and openai:
        try:
            print("[generate_text_with_openai] Llamando a OpenAI (Prompt final — exam + solutions)...")
            resp = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": PROMPT}],
                temperature=0.0,
                max_tokens=3500
            )
            text = resp["choices"][0]["message"]["content"]
            print(f"[generate_text_with_openai] OpenAI OK — longitud: {len(text)} chars")
            return text
        except Exception as e:
            print(f"[generate_text_with_openai] OpenAI error: {e}")

    # fallback (muy simple)
    return f"Asignatura: {subject}\nTema: {topic}\n\nPreguntas de alternativa:\n\n(Contenido fallback)\n\n<<SOLUTIONS>>\n\nSolucionario:\n\n(Contenido fallback)"


def save_docx_from_text(text: str, path: str) -> None:
    """
    Guardado minimalista: texto negro, con Asignatura/Tema en negrita,
    numeración de preguntas y opciones tal como vienen en el texto.
    Si python-docx falla, crea un archivo con la extensión .docx conteniendo el texto (fallback).
    """
    try:
        if Document is None:
            raise RuntimeError("python-docx no instalado")
        doc = Document()
        # aplicar estilo base
        try:
            style = doc.styles['Normal']
            font = style.font
            if Pt is not None:
                font.size = Pt(11)
            font.name = 'Calibri'
        except Exception:
            pass

        for raw_line in text.split("\n"):
            line = raw_line.rstrip()
            if not line:
                doc.add_paragraph("")  # blank
                continue

            # Asignatura: y Tema: en negrita (mantener en una linea)
            if line.startswith("Asignatura:") or line.startswith("Tema:"):
                p = doc.add_paragraph()
                run = p.add_run(line)
                run.bold = True
                continue

            # encabezados simples "Preguntas..." en negrita
            if line.lower().startswith("preguntas"):
                p = doc.add_paragraph()
                run = p.add_run(line)
                run.bold = True
                continue

            # numbering like "1) " (ej: "1) Enunciado")
            if len(line) >= 3 and line[:3].strip().isdigit() and line[2] == ')':
                p = doc.add_paragraph(line, style='List Number')
                continue

            # opciones "A. " o "A) " o "A. Opción"
            if len(line) >= 2 and (line[1] == '.' or line[1] == ')') and line[0].isalpha():
                p = doc.add_paragraph(line)
                continue

            # default: parrafo normal
            p = doc.add_paragraph(line)

        # intentar guardar como docx real
        doc.save(path)
        print(f"[save_docx_from_text] DOCX guardado: {path}")
    except Exception as e:
        # Fallback robusto: crear un .docx "texto" para que siempre haya un archivo descargable
        try:
            print(f"[save_docx_from_text] warning: python-docx falló ({e}), creando fallback .docx de texto")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"[save_docx_from_text] Fallback .docx escrito: {path}")
        except Exception as e2:
            print(f"[save_docx_from_text] Error escribiendo fallback .docx: {e2}")
            raise


def save_pdf_from_text(text: str, path: str) -> None:
    """
    Guarda un PDF con negritas para encabezados usando reportlab si está disponible;
    si no, crea un archivo de texto con extensión .pdf como fallback.
    """
    # import simpleSplit lazily (sólo si reportlab está instalado)
    try:
        from reportlab.lib.utils import simpleSplit
    except Exception:
        simpleSplit = None

    if canvas is None or letter is None or simpleSplit is None:
        # fallback: crear txt y también escribir archivo con extensión .pdf para pruebas
        try:
            with open(path, "w", encoding="utf-8") as f2:
                f2.write(text)
            print(f"[save_pdf_from_text] reportlab no instalado o simpleSplit ausente: generado fallback archivo con extensión .pdf: {path}")
            return
        except Exception as e:
            raise RuntimeError(f"Error fallback saving pdf-like file: {e}")

    c = canvas.Canvas(path, pagesize=letter)
    width, height = letter
    margin = 50
    max_width = width - 2 * margin
    y = height - margin

    # Tipos de fuente y tamaños
    normal_font = "Helvetica"
    bold_font = "Helvetica-Bold"
    size_title = 14
    size_heading = 12
    size_normal = 11
    leading = 14  # espacio vertical por línea

    paragraphs = text.split("\n\n")
    for paragraph in paragraphs:
        # para cada línea dentro del párrafo
        for raw_line in paragraph.split("\n"):
            line = raw_line.strip()
            if not line:
                y -= leading // 2
                continue

            # Detectar Asignatura / Tema -> título en negrita y tamaño mayor
            if line.startswith("Asignatura:") or line.startswith("Tema:"):
                font = bold_font
                fsize = size_title
            # encabezados 'Preguntas...' -> negrita heading
            elif line.lower().startswith("preguntas"):
                font = bold_font
                fsize = size_heading
            else:
                # numbering lines or options use normal
                font = normal_font
                fsize = size_normal

            wrapped = simpleSplit(line, font, fsize, max_width)
            for wline in wrapped:
                if y < margin + leading:
                    c.showPage()
                    y = height - margin
                c.setFont(font, fsize)
                c.drawString(margin, y, wline)
                y -= leading
        y -= leading // 2
    c.save()
    print(f"[save_pdf_from_text] PDF guardado con estilo: {path}")


# ---------- Core: generate and deliver (genera exam y soluciones por separado) ----------
def generate_and_deliver(order_id: str, payer_email: Optional[str] = None):
    print(f"[generate_and_deliver] START order {order_id} for {payer_email}")
    order = ORDERS.get(order_id, {})
    payload = order.get("payload", {}) if isinstance(order, dict) else {}
    subject = payload.get("subject", "Asignatura")
    topic = payload.get("topic", "Tema")
    mcq_count = int(payload.get("mcqCount", payload.get("mcq_count", 14) or 14))
    essay_count = int(payload.get("essayCount", payload.get("essay_count", 2) or 2))

    order["status"] = "processing"
    ORDERS[order_id] = order

    text = generate_text_with_openai(subject, topic, mcq_count, essay_count)

    # Separar usando marcador explícito <<SOLUTIONS>>
    exam_part = None
    solutions_part = None
    if "<<SOLUTIONS>>" in text:
        parts = text.split("<<SOLUTIONS>>", 1)
        exam_part = parts[0].strip()
        solutions_part = parts[1].strip()
    else:
        # Fallback: intentar buscar "Solucionario" o "Solucionario:"
        if "Solucionario" in text:
            idx = text.find("Solucionario")
            exam_part = text[:idx].strip()
            solutions_part = text[idx:].strip()
        else:
            # si no encontramos, guardamos todo en exam y solutions vacío
            exam_part = text.strip()
            solutions_part = "Solucionario no generado (fallback)."

    safe = order_id.replace("/", "_")
    exam_docx = os.path.join(FILES_PATH, f"{safe}-exam.docx")
    exam_pdf  = os.path.join(FILES_PATH, f"{safe}-exam.pdf")
    sol_docx  = os.path.join(FILES_PATH, f"{safe}-solutions.docx")
    sol_pdf   = os.path.join(FILES_PATH, f"{safe}-solutions.pdf")

    # Guardar archivos (usar save_docx_from_text y save_pdf_from_text)
    try:
        save_docx_from_text(exam_part, exam_docx)
        print(f"[generate_and_deliver] Exam DOCX saved: {exam_docx}")
    except Exception as e:
        print(f"[generate_and_deliver] Error saving exam DOCX: {e}")
    try:
        save_docx_from_text(solutions_part, sol_docx)
        print(f"[generate_and_deliver] Solutions DOCX saved: {sol_docx}")
    except Exception as e:
        print(f"[generate_and_deliver] Error saving solutions DOCX: {e}")

    try:
        save_pdf_from_text(exam_part, exam_pdf)
        print(f"[generate_and_deliver] Exam PDF saved: {exam_pdf}")
    except Exception as e:
        print(f"[generate_and_deliver] Error saving exam PDF: {e}")
    try:
        save_pdf_from_text(solutions_part, sol_pdf)
        print(f"[generate_and_deliver] Solutions PDF saved: {sol_pdf}")
    except Exception as e:
        print(f"[generate_and_deliver] Error saving solutions PDF: {e}")

    base = BASE_URL if str(BASE_URL).startswith("http") else f"https://{BASE_URL}"
    order["exam_pdf_url"] = f"{base}/files/{os.path.basename(exam_pdf)}"
    order["exam_docx_url"] = f"{base}/files/{os.path.basename(exam_docx)}"
    order["solutions_pdf_url"] = f"{base}/files/{os.path.basename(sol_pdf)}"
    order["solutions_docx_url"] = f"{base}/files/{os.path.basename(sol_docx)}"

    order["status"] = "ready"
    order["delivered_at"] = time.time()
    ORDERS[order_id] = order
    print(f"[generate_and_deliver] DONE order {order_id}. Exam: {order['exam_pdf_url']} Solutions: {order['solutions_pdf_url']}")


@app.get("/order-status")
async def order_status(order_id: str):
    """Wix puede consultar este endpoint para saber si el pedido está listo."""
    order = ORDERS.get(order_id)
    if not order:
        return {"status": "not_found"}
    return {
        "status": order.get("status"),
        "exam_pdf_url": order.get("exam_pdf_url"),
        "exam_docx_url": order.get("exam_docx_url"),
        "solutions_pdf_url": order.get("solutions_pdf_url"),
        "solutions_docx_url": order.get("solutions_docx_url"),
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

# Health-check simple (GET) para /mp-webhook — facilita depuración de webhooks
from fastapi.responses import JSONResponse

@app.get("/mp-webhook")
async def mp_webhook_get():
    # responde 200 para que Mercado Pago marque la URL accesible
    return JSONResponse({"ok": True, "note": "mp-webhook GET alive"})


# ---------- End of file ----------
