FROM python:3.12-slim

WORKDIR /app
COPY servicio_transacciones/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY servicio_transacciones /app/servicio_transacciones

EXPOSE 8000
# Varios workers para usar todos los nucleos (cada uno con su propio pool).
# Conexiones totales a la BD = workers x (pool_size + max_overflow): dimensionar contra max_connections.
CMD ["sh", "-c", "exec uvicorn servicio_transacciones.app.main:app --host 0.0.0.0 --port 8000 --workers \"${UVICORN_WORKERS:-5}\""]
