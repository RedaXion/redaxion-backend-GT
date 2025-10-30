# main.py (RedaXion) - actualizado: Redis + RQ + GCS uploads + signed URLs
import os
import time
import uuid
import threading
import inspect
import json
from typing import Optional
from datetime import timedelta

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
    from docx.shared import Pt
except Exception:
    Document = None
    Pt = None

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
except Exception:
    canvas = None
    letter = None

# Optional infra libs
try:
    import redis
except Exception:
    redis = None

try:
    from rq import Queue
except Exception:
    Queue = None

try:
    from google.cloud import storage
except Exception:
    storage = None

# ---------- Config (env vars) ----------
MP_ACCESS_TOKEN = (os.getenv("MP_ACCESS_TOKEN") or "").strip()
BASE_URL = os.getenv("BASE_URL") or ""
PORT = int(os.getenv("PORT", 8000))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ENABLE_SIMULATE = os.getenv("ENABLE_SIMULATE", "0") == "1"

REDIS_URL = os.getenv("REDIS_URL")  # ej: redis://:password@host:port/0

# GCS config
GCS_SERVICE_ACCOUNT_JSON = os.getenv("GCS_SERVICE_ACCOUNT_JSON")
GCS_BUCKET = os.getenv("GCS_BUCKET")
GCS_SIGNED_URL_EXPIRE_HOURS = int(os.getenv("GCS_SIGNED_URL_EXPIRE_HOURS", "24"))

# sanity checks (only warn; do not crash worker if some configs missing — allow simulate)
if not MP_ACCESS_TOKEN:
    print("[warning] MP_ACCESS_TOKEN NO configurado. Algunos endpoints MP pueden fallar.")

if not BASE_URL:
    BASE_URL = "https://TU_DOMINIO_RAILWAY"

if OPENAI_API_KEY and openai:
    try:
        openai.api_key = OPENAI_API_KEY
    except Exception:
        pass

# ---------- App ----------
app = FastAPI(title="RedaXion - Backend (Redis+GCS ready)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# keep mounting /files for compatibility if needed (no-op if not used)
FILES_PATH = "/tmp"
app.mount("/files", StaticFiles(directory=FILES_PATH), name="files")

# ---------- Persistence layer: Redis fallback ----------
_redis = None
if redis and REDIS_URL:
    try:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
        # quick ping
        _ = _redis.ping()
        print("[redis] conectado correctamente")
    except Exception as e:
        print(f"[redis] error conectando a REDIS_URL: {e}; usando memoria local ORDERS")

# simple in-memory fallback (only used if redis not configured)
ORDERS = {}

def save_order_to_store(order_id: str, order_obj: dict) -> None:
    """Save order to redis if available, otherwise in-memory."""
    if _redis:
        try:
            _redis.set(f"order:{order_id}", json.dumps(order_obj))
        except Exception as e:
            print(f"[save_order_to_store] Redis set error: {e}; fallback memoria")
            ORDERS[order_id] = order_obj
    else:
        ORDERS[order_id] = order_obj

def load_order_from_store(order_id: str) -> Optional[dict]:
    """Load order from redis or memory."""
    if _redis:
        try:
            raw = _redis.get(f"order:{order_id}")
            if not raw:
                return None
            return json.loads(raw)
        except Exception as e:
            print(f"[load_order_from_store] Redis get error: {e}; fallback memoria")
            return ORDERS.get(order_id)
    else:
        return ORDERS.get(order_id)

# ---------- RQ enqueue helper ----------
_rq_queue = None
if _redis and Queue:
    try:
        _rq_queue = Queue("reda", connection=_redis)
        print("[rq] queue 'reda' lista")
    except Exception as e:
        print(f"[rq] no se pudo inicializar queue: {e}")

def enqueue_generate_and_deliver(order_id: str) -> Optional[str]:
    """Enqueue job into RQ. Returns job id if ok, otherwise None."""
    if not _rq_queue:
        print("[enqueue] RQ queue no disponible")
        return None
    try:
        # enqueue call by function path; worker must have same module available
        job = _rq_queue.enqueue("main.generate_and_deliver", order_id, job_timeout=3600)
        print(f"[enqueue] job enqueued: {job.id}")
        return job.id
    except Exception as e:
        print(f"[enqueue] error enqueuing job: {e}")
        return None

# ---------- GCS client + helpers ----------
_gcs_client = None
if storage:
    try:
        if GCS_SERVICE_ACCOUNT_JSON:
            info = json.loads(GCS_SERVICE_ACCOUNT_JSON)
            _gcs_client = storage.Client.from_service_account_info(info)
        else:
            # attempt default credentials
            _gcs_client = storage.Client()
        print("[gcs] client inicializado")
    except Exception as e:
        print(f"[gcs] no se pudo inicializar client: {e}; GCS deshabilitado")
        _gcs_client = None
else:
    print("[gcs] google-cloud-storage no instalado; instala google-cloud-storage en requirements.txt")

def upload_file_to_gcs_and_get_signed_url(local_path: str, expire_hours: int = None) -> Optional[str]:
    if _gcs_client is None:
        print("[gcs] cliente no inicializado")
        return None
    if not GCS_BUCKET:
        print("[gcs] GCS_BUCKET no configurado")
        return None
    try:
        bucket = _gcs_client.bucket(GCS_BUCKET)
        blob_name = os.path.basename(local_path)
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(local_path)
        expire = timedelta(hours=expire_hours or GCS_SIGNED_URL_EXPIRE_HOURS)
        url = blob.generate_signed_url(expiration=expire, version="v4")
        print(f"[gcs] uploaded {local_path} -> {blob_name}")
        return url
    except Exception as e:
        print(f"[gcs] upload error: {e}")
        return None

# ---------- Helpers ----------
def generate_access_code():
    t = time.strftime("%y%m%d%H%M%S")
    random = uuid.uuid4().hex[:4].upper()
    return f"RX-{t}-{random}"

# ---------- Endpoints ----------
@app.post("/create-preference")
async def create_preference(req: Request):
    data = await req.json()
    order_id = data.get("order_id") or f"ORD-{uuid.uuid4().hex[:8].upper()}"
    email = data.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Falta email")

    order_obj = {
        "email": email,
        "payload": data,
        "status": "pending",
        "created_at": time.time()
    }
    save_order_to_store(order_id, order_obj)

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

    order_obj["preference_id"] = preference_id
    save_order_to_store(order_id, order_obj)

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

    order = load_order_from_store(external_ref) or {}
    order["status"] = "paid"
    order["payment_id"] = payment_id
    order["amount"] = amount
    order["payer_email"] = payer_email
    save_order_to_store(external_ref, order)

    code = generate_access_code()
    order["access_code"] = code
    save_order_to_store(external_ref, order)

    # enqueue or fallback to thread
    jobid = None
    if _rq_queue:
        try:
            jobid = enqueue_generate_and_deliver(external_ref)
            print(f"[mp-webhook] enqueued job {jobid} for order {external_ref}")
        except Exception as e:
            print(f"[mp-webhook] enqueue failed: {e}; falling back to thread")
            threading.Thread(target=_run_generate_with_correct_args, args=(external_ref, payer_email), daemon=True).start()
    else:
        threading.Thread(target=_run_generate_with_correct_args, args=(external_ref, payer_email), daemon=True).start()

    return {"ok": True, "order": external_ref, "code": code, "jobid": jobid}


@app.get("/order-status")
async def order_status(order_id: str):
    order = load_order_from_store(order_id)
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

# Health-check simple (GET) para /mp-webhook — facilita depuración de webhooks
from fastapi.responses import JSONResponse
@app.get("/mp-webhook")
async def mp_webhook_get():
    return JSONResponse({"ok": True, "note": "mp-webhook GET alive"})

# ---------- Document generation helpers ----------
def generate_text_with_openai(subject: str, topic: str, mcq_count: int = 14, essay_count: int = 2) -> str:
    PROMPT = f"""
Eres RedaXion. Produce DOS BLOQUES separados exactamente por la línea:
<<SOLUTIONS>>
... (PROMPT REDACTADO PARA EJEMPLO) ...
"""
    if OPENAI_API_KEY and openai:
        try:
            resp = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": PROMPT}],
                temperature=0.0,
                max_tokens=3500
            )
            return resp["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[generate_text_with_openai] error: {e}")

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
        print(f"[save_docx_from_text] warning python-docx failed: {e}")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"[save_docx_from_text] fallback docx escrito: {path}")
        except Exception as e2:
            print(f"[save_docx_from_text] fallback failed: {e2}")
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
            print(f"[save_pdf_from_text] fallback pdf escrito: {path}")
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
    leading = 14

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

# ---------- Core: generate_and_deliver (acepta kwargs extras) ----------
def _run_generate_with_correct_args(order_id: str, payer_email: Optional[str]):
    try:
        fn = generate_and_deliver
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

def generate_and_deliver(order_id: str, payer_email: Optional[str] = None, **kwargs):
    """
    Genera los archivos, los sube a GCS (si está configurado) y actualiza el order en Redis/memoria.
    Acepta kwargs extras para evitar errores con RQ que pasan timeouts u otros metadatos.
    """
    print(f"[generate_and_deliver] START order {order_id} for {payer_email}")
    order = load_order_from_store(order_id) or {}
    payload = order.get("payload", {}) if isinstance(order, dict) else {}
    subject = payload.get("subject", "Asignatura")
    topic = payload.get("topic", "Tema")
    mcq_count = int(payload.get("mcqCount", payload.get("mcq_count", 14) or 14))
    essay_count = int(payload.get("essayCount", payload.get("essay_count", 2) or 2))

    order["status"] = "processing"
    save_order_to_store(order_id, order)

    text = generate_text_with_openai(subject, topic, mcq_count, essay_count)

    # Separar usando marcador explícito <<SOLUTIONS>>
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
    except Exception as e:
        print(f"[generate_and_deliver] Error saving exam DOCX: {e}")
    try:
        save_docx_from_text(solutions_part, sol_docx)
    except Exception as e:
        print(f"[generate_and_deliver] Error saving solutions DOCX: {e}")

    try:
        save_pdf_from_text(exam_part, exam_pdf)
    except Exception as e:
        print(f"[generate_and_deliver] Error saving exam PDF: {e}")
    try:
        save_pdf_from_text(solutions_part, sol_pdf)
    except Exception as e:
        print(f"[generate_and_deliver] Error saving solutions PDF: {e}")

    # Subir a GCS si está configurado; retornar signed URLs
    exam_pdf_url = upload_file_to_gcs_and_get_signed_url(exam_pdf) if _gcs_client else None
    solutions_pdf_url = upload_file_to_gcs_and_get_signed_url(sol_pdf) if _gcs_client else None
    exam_docx_url = upload_file_to_gcs_and_get_signed_url(exam_docx) if _gcs_client else None
    solutions_docx_url = upload_file_to_gcs_and_get_signed_url(sol_docx) if _gcs_client else None

    # Guardar URLs en order y persistir
    order["exam_pdf_url"] = exam_pdf_url
    order["exam_docx_url"] = exam_docx_url
    order["solutions_pdf_url"] = solutions_pdf_url
    order["solutions_docx_url"] = solutions_docx_url

    order["status"] = "ready"
    order["delivered_at"] = time.time()
    save_order_to_store(order_id, order)

    print(f"[generate_and_deliver] DONE order {order_id}. Exam: {exam_pdf_url} Solutions: {solutions_pdf_url}")

# ---------- Optional simulate endpoint (only if ENABLE_SIMULATE=1) ----------
if ENABLE_SIMULATE:
    @app.post("/simulate-paid")
    async def simulate_paid(request: Request):
        payload = await request.json()
        order_id = payload.get("order_id") or f"SIM-{int(time.time())}"
        payer_email = payload.get("payer_email") or "cliente@ejemplo.com"

        order = load_order_from_store(order_id) or {}
        order["status"] = "paid"
        order["payer_email"] = payer_email
        order["payment_id"] = f"SIM-{uuid.uuid4().hex[:8]}"
        order["access_code"] = generate_access_code()
        save_order_to_store(order_id, order)

        # enqueue or thread fallback
        if _rq_queue:
            jid = enqueue_generate_and_deliver(order_id)
            if not jid:
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
