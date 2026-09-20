FROM python:3.12-slim

WORKDIR /app

COPY servicio_bancs/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY servicio_bancs /app/servicio_bancs

CMD ["uvicorn", "servicio_bancs.app.main:app", "--host", "0.0.0.0", "--port", "8001"]