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

# Optional AI + document libs (with fallbacks)
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
except Exception:
    canvas = None
    letter = None

# ---------- Config (env vars) ----------
MP_ACCESS_TOKEN = (os.getenv("MP_ACCESS_TOKEN") or "").strip()  # token Mercado Pago (TEST-... or prod)
BASE_URL = os.getenv("BASE_URL") or ""  # e.g. "https://mi-proyecto.up.railway.app"
PORT = int(os.getenv("PORT", 8000))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # opcional, para generación de texto con IA
ENABLE_SIMULATE = os.getenv("ENABLE_SIMULATE", "0") == "1"  # habilita /simulate-paid si "1"
REDIS_URL = os.getenv("REDIS_URL")

if not MP_ACCESS_TOKEN:
    raise RuntimeError("Por favor configura MP_ACCESS_TOKEN en variables de entorno")

if not BASE_URL:
    # No se frena, pero es recomendable configurar BASE_URL en Railway.
    BASE_URL = "https://TU_DOMINIO_RAILWAY"

if OPENAI_API_KEY and openai:
    try:
        openai.api_key = OPENAI_API_KEY
    except Exception:
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

# Try to import enqueue helper - tasks.py must exist in repo
try:
    from tasks import enqueue_generate_and_deliver
except Exception:
    enqueue_generate_and_deliver = None
    print("[startup] tasks.enqueue_generate_and_deliver not available; will fallback to thread execution.")

# ---------- Helper: call generate_and_deliver with correct args ----------
def _run_generate_with_correct_args(order_id: str, payer_email: Optional[str]):
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
    data = await req.json()
    order_id = data.get("order_id") or f"ORD-{uuid.uuid4().hex[:8].upper()}"
    email = data.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Falta email")

    ORDERS[order_id] = {
        "email": email,
        "payload": data,
        "status": "pending",
        "created_at": time.time()
    }

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
    try:
        body = await req.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    payment_id = None
    if isinstance(body, dict):
        if "data" in body and isinstance(body["data"], dict) and "id" in body["data"]:
            payment_id = body["data"]["id"]
        elif "id" in body:
            payment_id = body["id"]

    if not payment_id:
        return {"ok": True, "note": "no payment id"}

    r = requests.get(f"https://api.mercadopago.com/v1/payments/{payment_id}",
                     headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"})
    if r.status_code != 200:
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

    order = ORDERS.get(external_ref)
    if not order:
        ORDERS[external_ref] = {"email": payer_email, "payload": {}, "status": "paid", "created_at": time.time()}
        order = ORDERS[external_ref]

    order["status"] = "paid"
    order["payment_id"] = payment_id
    order["amount"] = amount
    order["payer_email"] = payer_email

    code = generate_access_code()
    order["access_code"] = code

    # Lanzar generación del examen en background via RQ enqueue (fallback to thread if not available)
    if enqueue_generate_and_deliver:
        try:
            jobid = enqueue_generate_and_deliver(external_ref)
            print(f"[mp-webhook] enqueued job {jobid} for order {external_ref}")
        except Exception as e:
            print(f"[mp-webhook] enqueue failed: {e}; falling back to thread")
            threading.Thread(target=_run_generate_with_correct_args, args=(external_ref, payer_email), daemon=True).start()
    else:
        threading.Thread(target=_run_generate_with_correct_args, args=(external_ref, payer_email), daemon=True).start()

    return {"ok": True, "order": external_ref, "code": code}

# ---------- Document generation helpers ----------
def generate_text_with_openai(subject: str, topic: str, mcq_count: int = 14, essay_count: int = 2) -> str:
    # Prompt omitted here for safety; original prompt is used in production code.
    PROMPT = "REDA_PROMPT_PLACEHOLDER"
    if OPENAI_API_KEY and openai:
        try:
            print("[generate_text_with_openai] Llamando a OpenAI (Prompt final)...")
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

    return f"Asignatura: {subject}\nTema: {topic}\n\nPreguntas de alternativa:\n\n(Contenido fallback)\n\n<<SOLUTIONS>>\n\nSolucionario:\n\n(Contenido fallback)"

def save_docx_from_text(text: str, path: str) -> None:
    try:
        if Document is None:
            raise RuntimeError("python-docx no instalado")
        doc = Document()
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

            if line.startswith("Asignatura:") or line.startswith("Tema:"):
                p = doc.add_paragraph()
                run = p.add_run(line)
                run.bold = True
                continue

            if line.lower().startswith("preguntas"):
                p = doc.add_paragraph()
                run = p.add_run(line)
                run.bold = True
                continue

            if len(line) >= 3 and line[:3].strip().isdigit() and line[2] == ')':
                p = doc.add_paragraph(line, style='List Number')
                continue

            if len(line) >= 2 and (line[1] == '.' or line[1] == ')') and line[0].isalpha():
                p = doc.add_paragraph(line)
                continue

            p = doc.add_paragraph(line)

        doc.save(path)
        print(f"[save_docx_from_text] DOCX guardado: {path}")
    except Exception as e:
        try:
            print(f"[save_docx_from_text] warning: python-docx falló ({e}), creando fallback .docx de texto")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"[save_docx_from_text] Fallback .docx escrito: {path}")
        except Exception as e2:
            print(f"[save_docx_from_text] Error escribiendo fallback .docx: {e2}")
            raise

def save_pdf_from_text(text: str, path: str) -> None:
    try:
        from reportlab.lib.utils import simpleSplit
    except Exception:
        simpleSplit = None

    if canvas is None or letter is None or simpleSplit is None:
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

    normal_font = "Helvetica"
    bold_font = "Helvetica-Bold"
    size_title = 14
    size_heading = 12
    size_normal = 11
    leading = 14  # espacio vertical por línea

    paragraphs = text.split("\n\n")
    for paragraph in paragraphs:
        for raw_line in paragraph.split("\n"):
            line = raw_line.strip()
            if not line:
                y -= leading // 2
                continue

            if line.startswith("Asignatura:") or line.startswith("Tema:"):
                font = bold_font
                fsize = size_title
            elif line.lower().startswith("preguntas"):
                font = bold_font
                fsize = size_heading
            else:
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
        if "Solucionario" in text:
            idx = text.find("Solucionario")
            exam_part = text[:idx].strip()
            solutions_part = text[idx:].strip()
        else:
            exam_part = text.strip()
            solutions_part = "Solucionario no generado (fallback)."

    safe = order_id.replace("/", "_")
    exam_docx = os.path.join(FILES_PATH, f"{safe}-exam.docx")
    exam_pdf  = os.path.join(FILES_PATH, f"{safe}-exam.pdf")
    sol_docx  = os.path.join(FILES_PATH, f"{safe}-solutions.docx")
    sol_pdf   = os.path.join(FILES_PATH, f"{safe}-solutions.pdf")

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

        # enqueue or fallback to thread
        if enqueue_generate_and_deliver:
            try:
                jobid = enqueue_generate_and_deliver(order_id)
                print(f"[simulate-paid] enqueued job {jobid} for {order_id}")
            except Exception as e:
                print(f"[simulate-paid] enqueue failed: {e}; falling back to thread")
                threading.Thread(target=_run_generate_with_correct_args, args=(order_id, payer_email), daemon=True).start()
        else:
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
