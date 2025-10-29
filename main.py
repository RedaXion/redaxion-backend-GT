import os
import time
import uuid
import threading
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests

# Config desde variables de entorno (configúralas en Railway)
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")  # token Mercado Pago
BASE_URL = os.getenv("BASE_URL")  # e.j. https://mi-proyecto.railway.app  (IMPORTANTE)
PORT = int(os.getenv("PORT", 8000))

if not MP_ACCESS_TOKEN:
    raise RuntimeError("Por favor configura MP_ACCESS_TOKEN en variables de entorno")

if not BASE_URL:
    # No se frena, pero te aconsejo configurar BASE_URL en Railway para que el webhook use URL correcta.
    BASE_URL = "https://TU_DOMINIO_RAILWAY"  # reemplaza si no configuras env var

app = FastAPI(title="RedaXion - Backend minimo")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store temporal en memoria (simple)
ORDERS = {}  # order_id -> {email, payload, status, created_at, code}

def generate_access_code():
    t = time.strftime("%y%m%d%H%M%S")
    random = uuid.uuid4().hex[:4].upper()
    return f"RX-{t}-{random}"

@app.post("/create-preference")
async def create_preference(req: Request):
    """
    Wix llamará a este endpoint con JSON:
    { order_id, email, subject, topic, bloom, mcqCount, essayCount, includeSolutions }
    Devuelve: { init_point, preference_id }
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

    # Guardamos referencia
    ORDERS[order_id]["preference_id"] = preference_id

    return {"init_point": init_point, "preference_id": preference_id, "order_id": order_id}


@app.post("/mp-webhook")
async def mp_webhook(req: Request):
    """
    Mercado Pago notificará aquí (webhook). Validamos la transacción con la API de MP.
    """
    body = await req.json()
    # Mercado Pago suele enviar data.id en body
    payment_id = None
    if isinstance(body, dict):
        # Manejo varias formas
        if "data" in body and isinstance(body["data"], dict) and "id" in body["data"]:
            payment_id = body["data"]["id"]
        elif "id" in body:
            payment_id = body["id"]

    if not payment_id:
        return {"ok": True, "note": "no payment id"}

    # Validar pago con la API de Mercado Pago
    r = requests.get(f"https://api.mercadopago.com/v1/payments/{payment_id}", headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"})
    if r.status_code != 200:
        return {"error": "No se pudo validar pago", "details": r.text}

    payment = r.json()
    if payment.get("status") != "approved":
        return {"ok": True, "status": payment.get("status")}

    external_ref = payment.get("external_reference") or (payment.get("metadata") or {}).get("order_id")
    payer_email = payment.get("payer", {}).get("email")
    amount = payment.get("transaction_amount")

    if not external_ref:
        return {"ok": False, "error": "external_reference missing"}

    # Verificar que el order exista
    order = ORDERS.get(external_ref)
    if not order:
        # Podemos crear registro si no existe, pero mejor reportarlo
        ORDERS[external_ref] = {"email": payer_email, "payload": {}, "status": "paid", "created_at": time.time()}
        order = ORDERS[external_ref]

    order["status"] = "paid"
    order["payment_id"] = payment_id
    order["amount"] = amount
    order["payer_email"] = payer_email

    # Generar código y guardar
    code = generate_access_code()
    order["access_code"] = code

    # Lanzar generación del examen en background (no bloquea el webhook)
    threading.Thread(target=generate_and_deliver, args=(external_ref,)).start()

    return {"ok": True, "order": external_ref, "code": code}


def generate_and_deliver(order_id: str):
    """
    Función de ejemplo que se ejecuta en background:
    - llama a OpenAI (aquí debes implementar la llamada real)
    - genera DOCX/PDF
    - sube a storage o guarda en filesystem accesible
    - envía email con links al cliente
    """
    order = ORDERS.get(order_id)
    if not order:
        return

    # mark processing
    order["status"] = "processing"

    # --- Aquí debes poner la lógica real de generación con OpenAI ---
    # Por ahora simulamos (espera 5s)
    time.sleep(5)

    # Simula archivos generados (en tu implementación reemplaza por archivos reales)
    pdf_url = f"{BASE_URL}/files/{order_id}.pdf"   # ideal: subir a S3/GCS y obtener URL real
    docx_url = f"{BASE_URL}/files/{order_id}.docx"

    # Guardar links en order
    order["pdf_url"] = pdf_url
    order["docx_url"] = docx_url
    order["status"] = "ready"

    # Aquí envía un email real usando SendGrid / SMTP con pdf_url y docx_url
    # send_email(order["email"], "Tu examen RedaXion está listo", f"Descarga aquí: {pdf_url}")

    print(f"[generate_and_deliver] Order {order_id} listo. Links: {pdf_url} {docx_url}")

@app.get("/order-status")
async def order_status(order_id: str):
    """Wix puede consultar este endpoint para saber si el pedido está listo."""
    order = ORDERS.get(order_id)
    if not order:
        return {"status": "not_found"}
    return {"status": order.get("status"), "pdf_url": order.get("pdf_url"), "docx_url": order.get("docx_url"), "access_code": order.get("access_code")}
