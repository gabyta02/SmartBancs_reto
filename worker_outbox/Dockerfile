FROM python:3.12-slim

WORKDIR /app

COPY worker_outbox/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY worker_outbox /app/worker_outbox

CMD ["python", "-u", "-m", "worker_outbox.app.main"]