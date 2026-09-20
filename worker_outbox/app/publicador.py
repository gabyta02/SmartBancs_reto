import json
import logging
import time
import pika
from opentelemetry import trace
from opentelemetry.propagate import extract, inject

from worker_outbox.app.database.conexion import (
    crear_conexion_rabbit,
)
from worker_outbox.app.configuracion import settings
from worker_outbox.app.observabilidad.metricas import (
    RABBITMQ_PUBLICACIONES_TOTAL, RABBITMQ_PUBLICACION_DURACION_SECONDS,
)

tracer = trace.get_tracer("smartbancs.worker_outbox")
logger = logging.getLogger("smartbancs.worker_outbox")



def construir_mensaje_ia(
    evento: dict,
    trace_context: dict | None = None,
) -> dict:

    carga = evento.get(
        "carga_util",
        {},
    )

    mensaje = {
        "id_evento": str(
            evento["id_eventos"]
        ),
        "id_transaccion": str(
            evento["transaccion_id"]
        ),
        "tipo_evento": evento[
            "tipo_evento"
        ],
        "datos": {clave: valor for clave, valor in carga.items() if clave != "trace_context"},
    }
    if trace_context is not None:
        mensaje["trace_context"] = trace_context
    return mensaje


def publicar_evento_ia(
    evento: dict,
) -> None:

    carga = evento.get("carga_util", {})
    contexto_padre = extract(carga.get("trace_context", {}))
    with tracer.start_as_current_span(
        "worker_outbox.publicar_rabbitmq", context=contexto_padre
    ) as span:
        span.set_attribute("messaging.system", "rabbitmq")
        span.set_attribute("messaging.destination.name", settings.rabbitmq_queue_ia)
        span.set_attribute("messaging.operation.name", "publish")
        span.set_attribute("smartbancs.event.type", evento["tipo_evento"])

        contexto_mensaje = {}
        inject(contexto_mensaje)
        mensaje = construir_mensaje_ia(evento, contexto_mensaje)
        conexion = None
        try:
            conexion = crear_conexion_rabbit()
            canal = conexion.channel()
            canal.queue_declare(queue=settings.rabbitmq_queue_ia, durable=True)
            canal.confirm_delivery()
            inicio_publicacion = time.perf_counter()
            try:
                canal.basic_publish(
                exchange="",
                routing_key=settings.rabbitmq_queue_ia,
                body=json.dumps(mensaje, default=str),
                properties=pika.BasicProperties(
                    delivery_mode=2,
                    content_type="application/json",
                ),
                mandatory=True,
                )
            finally:
                RABBITMQ_PUBLICACION_DURACION_SECONDS.observe(time.perf_counter() - inicio_publicacion)
            RABBITMQ_PUBLICACIONES_TOTAL.labels(resultado="ok").inc()
        except Exception as exc:
            RABBITMQ_PUBLICACIONES_TOTAL.labels(resultado="error").inc()
            logger.error("Error publicando en RabbitMQ", extra={
                "evento": "rabbitmq_publicacion_error",
                "trace_id": format(span.get_span_context().trace_id, "032x"),
                "id_transaccion": str(evento["transaccion_id"]),
                "tipo_evento": evento["tipo_evento"], "tipo_error": type(exc).__name__,
            })
            raise
        finally:
            if conexion is not None and conexion.is_open:
                conexion.close()
