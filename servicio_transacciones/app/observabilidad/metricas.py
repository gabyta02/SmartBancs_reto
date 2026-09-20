import os

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
)


def worker_actual() -> str:
    """Identifica el proceso Uvicorn que emitio la muestra."""
    return str(os.getpid())


THREADPOOL_QUEUE_SECONDS = Histogram(
    "smartbancs_threadpool_queue_seconds",
    "Tiempo desde middleware hasta entrar en la ruta sincrona; incluye trabajo ASGI previo",
    ["worker"],
)

THREADPOOL_TOKENS_TOTAL = Gauge(
    "smartbancs_threadpool_tokens_total",
    "Tokens totales del limitador AnyIO en este worker",
    ["worker"],
)

THREADPOOL_TOKENS_BORROWED = Gauge(
    "smartbancs_threadpool_tokens_borrowed",
    "Tokens prestados del limitador AnyIO en este worker",
    ["worker"],
)

THREADPOOL_TASKS_WAITING = Gauge(
    "smartbancs_threadpool_tasks_waiting",
    "Tareas esperando un token AnyIO en este worker",
    ["worker"],
)

DB_POOL_SEMAFORO_WAIT_SECONDS = Histogram(
    "smartbancs_db_pool_semaforo_wait_seconds",
    "Tiempo para obtener un permiso del semaforo del pool",
    ["worker"],
)

DB_POOL_GETCONN_SECONDS = Histogram(
    "smartbancs_db_pool_getconn_seconds",
    "Tiempo dentro de ThreadedConnectionPool.getconn",
    ["worker"],
)

DB_POOL_PUTCONN_SECONDS = Histogram(
    "smartbancs_db_pool_putconn_seconds",
    "Tiempo dentro de ThreadedConnectionPool.putconn",
    ["worker"],
)

DB_SQL_DURACION_SECONDS = Histogram(
    "smartbancs_db_sql_duracion_seconds",
    "Duracion de operaciones SQL, incluido fetch y adaptacion local",
    ["worker", "operacion"],
)

RUTA_TRANSACCION_DURACION_SECONDS = Histogram(
    "smartbancs_ruta_transaccion_duracion_seconds",
    "Duracion interna de POST /transacciones en el threadpool",
    ["worker"],
)


TRANSACCIONES_TOTAL = Counter(
    "smartbancs_transacciones_total",
    "Cantidad total de transferencias procesadas",
    ["estado"],
)


TRANSACCIONES_REPRODUCIDAS_TOTAL = Counter(
    "smartbancs_transacciones_reproducidas_total",
    "Cantidad de transferencias reproducidas por idempotencia",
)


ERRORES_TRANSACCIONES_TOTAL = Counter(
    "smartbancs_transacciones_errores_total",
    "Cantidad de errores procesando transferencias",
    ["tipo"],
)


DURACION_TRANSFERENCIA = Histogram(
    "smartbancs_transferencia_duracion_seconds",
    "Tiempo total de procesamiento de una transferencia",
    buckets=(
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1,
        2,
        5,
    ),
)


DB_TIMEOUTS_TOTAL = Counter(
    "smartbancs_db_timeouts_total",
    "Cantidad de timeouts detectados en PostgreSQL",
)


DB_DEADLOCKS_TOTAL = Counter(
    "smartbancs_db_deadlocks_total",
    "Cantidad de deadlocks detectados en PostgreSQL",
)

DB_LOCK_WAIT_SECONDS = Histogram(
    "smartbancs_db_lock_wait_seconds",
    "Tiempo de espera al bloquear cuentas en PostgreSQL",
)

DB_OPERACIONES_TOTAL = Counter(
    "smartbancs_db_operaciones_total",
    "Operaciones de base de datos por resultado",
    ["operacion", "resultado"],
)

DB_OPERACION_DURACION_SECONDS = Histogram(
    "smartbancs_db_operacion_duracion_seconds",
    "Duracion de operaciones de base de datos",
    ["operacion"],
)

DB_REINTENTOS_TOTAL = Counter(
    "smartbancs_db_reintentos_total",
    "Reintentos completos de transferencias por SQLSTATE",
    ["sqlstate"],
)

DB_POOL_AGOTADO_TOTAL = Counter(
    "smartbancs_db_pool_agotado_total",
    "Solicitudes sin conexion disponible en el pool",
)

DB_POOL_ESPERA_SECONDS = Histogram(
    "smartbancs_db_pool_espera_seconds",
    "Tiempo hasta adquirir un slot del pool",
)

DB_POOL_ADQUISICIONES_TOTAL = Counter(
    "smartbancs_db_pool_adquisiciones_total",
    "Conexiones adquiridas del pool",
)

DB_POOL_TIMEOUTS_TOTAL = Counter(
    "smartbancs_db_pool_timeouts_total",
    "Solicitudes que agotaron el plazo de espera del pool",
)

DB_POOL_PRESTADAS = Gauge(
    "smartbancs_db_pool_prestadas",
    "Conexiones actualmente prestadas por este worker",
)

DB_POOL_RECHAZOS_TOTAL = Counter(
    "smartbancs_db_pool_rechazos_total",
    "Solicitudes rechazadas por el pool o su cola",
    ["motivo"],
)

HTTP_ERRORES_TOTAL = Counter(
    "smartbancs_http_errores_total",
    "Errores HTTP de POST /transacciones por causa (cardinalidad acotada)",
    ["worker", "endpoint", "status", "tipo", "motivo"],
)


def registrar_error_http(status: int, tipo: str, motivo: str) -> None:
    HTTP_ERRORES_TOTAL.labels(
        worker=worker_actual(), endpoint="/transacciones",
        status=str(status), tipo=tipo, motivo=motivo,
    ).inc()


DB_TRANSACCION_DURACION_SECONDS = Histogram(
    "smartbancs_db_transaccion_duracion_seconds",
    "Duracion de cada intento: incluye adquisicion de conexion y trabajo posterior al commit",
)
