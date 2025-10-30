# tasks.py - RQ enqueue helper for RedaXion (robust)
import os
from redis import Redis
from rq import Queue
from datetime import datetime

redis_url = os.getenv("REDIS_URL")
if redis_url:
    # decode_responses=True hace que redis devuelva strings en vez de bytes
    redis_conn = Redis.from_url(redis_url, decode_responses=True)
else:
    # fallback local (solo para desarrollo)
    redis_conn = Redis(decode_responses=True)

# Cola 'reda' con timeout por defecto de 1 hora
q = Queue("reda", connection=redis_conn, default_timeout=3600)

def enqueue_generate_and_deliver(order_id: str):
    """
    Encola el job que ejecuta main.generate_and_deliver(order_id).
    Usamos el path en string para evitar ciclos de importación en algunos deploys.
    job_timeout asegura que el worker mate jobs que se cuelguen después de 3600s.
    """
    job = q.enqueue("main.generate_and_deliver", order_id, job_timeout=3600)
    print(f"[tasks] enqueued job {job.id} for order {order_id} at {datetime.utcnow().isoformat()}")
    return job.id

# --- Alternative (commented): enqueue by function reference (if no circular imports) ---
# from main import generate_and_deliver
# def enqueue_generate_and_deliver(order_id: str):
#     job = q.enqueue(generate_and_deliver, order_id, job_timeout=3600)
#     print(f"[tasks] enqueued job {job.id} for order {order_id} at {datetime.utcnow().isoformat()}")
#     return job.id
