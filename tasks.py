# tasks.py - RQ enqueue helper for RedaXion
import os
from redis import Redis
from rq import Queue
from datetime import datetime

redis_url = os.getenv("REDIS_URL")
if not redis_url:
    redis_conn = Redis()
else:
    redis_conn = Redis.from_url(redis_url)

q = Queue("reda", connection=redis_conn)

def enqueue_generate_and_deliver(order_id: str):
    job = q.enqueue("main.generate_and_deliver", order_id, enqueue_timeout=3600, timeout=3600)
    print(f"[tasks] enqueued job {job.id} for order {order_id} at {datetime.utcnow().isoformat()}")
    return job.id
