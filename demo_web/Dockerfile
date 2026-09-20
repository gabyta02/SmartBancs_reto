FROM python:3.12-slim

WORKDIR /app

COPY demo_web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY demo_web /app/demo_web
# El ETL se ejecuta tal cual (etl.transformar.transformar), sin duplicar logica.
COPY etl /app/etl

EXPOSE 8080

CMD ["uvicorn", "demo_web.app:app", "--host", "0.0.0.0", "--port", "8080"]
