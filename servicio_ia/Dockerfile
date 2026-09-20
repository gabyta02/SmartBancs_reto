FROM python:3.12-slim

WORKDIR /app

COPY servicio_ia/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY servicio_ia /app/servicio_ia

CMD ["uvicorn", "servicio_ia.app.main:app", "--host", "0.0.0.0", "--port", "8002"]