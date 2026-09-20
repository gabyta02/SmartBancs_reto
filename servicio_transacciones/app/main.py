import time
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from servicio_transacciones.app.database.conexion import PoolAgotado
from servicio_transacciones.app.configuracion import settings
from servicio_transacciones.app.observabilidad.metricas import (
    DB_POOL_AGOTADO_TOTAL,
    THREADPOOL_TASKS_WAITING,
    THREADPOOL_TOKENS_BORROWED,
    THREADPOOL_TOKENS_TOTAL,
    registrar_error_http,
    worker_actual,
)

from servicio_transacciones.app.observabilidad.logging import (
    configurar_logging,
)

from servicio_transacciones.app.observabilidad.trazabilidad import (
    establecer_trace_id,
    generar_trace_id,
)

from servicio_transacciones.app.rutas.transferencias import (
    router as transferencias_router,
)

from servicio_transacciones.app.rutas.cuentas import (
    router as cuentas_router,
)

from servicio_transacciones.app.observabilidad.opentelemetry import (
    configurar_opentelemetry,
)

from prometheus_client import make_asgi_app

configurar_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    anyio.to_thread.current_default_thread_limiter().total_tokens = (
        settings.anyio_thread_tokens
    )
    yield


app = FastAPI(
    title="SmartBancs - Servicio de Transacciones",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(PoolAgotado)
async def pool_agotado_handler(request: Request, exc: PoolAgotado):
    DB_POOL_AGOTADO_TOTAL.inc()
    registrar_error_http(503, "pool", getattr(exc, "motivo", "desconocido"))
    return JSONResponse(
        status_code=503,
        content={"detail": "Servicio temporalmente saturado. Reintente con la misma clave de idempotencia."},
        headers={"Retry-After": "1"},
    )

configurar_opentelemetry(
    app
)

metrics_app = make_asgi_app()

app.mount(
    "/metrics",
    metrics_app,
)

app.include_router(
    transferencias_router,
)

app.include_router(
    cuentas_router,
)


@app.middleware("http")
async def middleware_trace_id(
    request: Request,
    call_next,
):
    if request.method == "POST" and request.url.path == "/transacciones":
        request.scope["smartbancs_request_inicio"] = time.perf_counter()

    def muestrear_threadpool():
        stats = anyio.to_thread.current_default_thread_limiter().statistics()
        worker = worker_actual()
        THREADPOOL_TOKENS_TOTAL.labels(worker=worker).set(stats.total_tokens)
        THREADPOOL_TOKENS_BORROWED.labels(worker=worker).set(stats.borrowed_tokens)
        THREADPOOL_TASKS_WAITING.labels(worker=worker).set(stats.tasks_waiting)

    muestrear_threadpool()
    trace_id = request.headers.get(
        "X-Trace-Id"
    )

    if not trace_id:
        trace_id = generar_trace_id()

    establecer_trace_id(
        trace_id
    )

    es_transferencia = request.method == "POST" and request.url.path == "/transacciones"
    try:
        response = await call_next(request)
    except Exception as exc:
        # Solo cuenta lo que llegara como 500 sin registrar; no altera la respuesta.
        if es_transferencia and not getattr(exc, "error_http_registrado", False):
            registrar_error_http(500, "interno", "excepcion_no_controlada")
        raise
    finally:
        muestrear_threadpool()

    if es_transferencia and response.status_code == 422:
        registrar_error_http(422, "validacion", "validacion")

    response.headers[
        "X-Trace-Id"
    ] = trace_id

    return response


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "transacciones",
    }
