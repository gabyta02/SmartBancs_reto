import json
import logging
import os
import time

import pika
from opentelemetry import trace
from opentelemetry.propagate import extract
from prometheus_client import start_http_server
from servicio_ia.app.observabilidad.metricas import (
    IA_ANALISIS_TOTAL, IA_DURACION_SECONDS, IA_MENSAJES_TOTAL,
    IA_PERSISTENCIA_DURACION_SECONDS,
)
from servicio_ia.app.observabilidad.logging import configurar_logging

from servicio_ia.app.observabilidad.opentelemetry import configurar_opentelemetry

from servicio_ia.app.esquemas.analisis import (
    SolicitudAnalisis,
)
from servicio_ia.app.servicios.recomendador import (
    analizar_transaccion,
)

from servicio_ia.app.database.conexion import (
    crear_conexion,
)

from servicio_ia.app.repositorios.analisis import (
    guardar_analisis,
)

RABBITMQ_HOST = os.getenv(
    "RABBITMQ_HOST",
    "rabbitmq",
)

RABBITMQ_PORT = int(
    os.getenv(
        "RABBITMQ_PORT",
        "5672",
    )
)

RABBITMQ_USER = os.getenv(
    "RABBITMQ_USER",
    "smartbancs",
)

RABBITMQ_PASSWORD = os.getenv(
    "RABBITMQ_PASSWORD",
    "smartbancs123",
)

RABBITMQ_QUEUE_IA = os.getenv(
    "RABBITMQ_QUEUE_IA",
    "cola_ia",
)

tracer = trace.get_tracer("smartbancs.consumidor_ia")
logger = logging.getLogger("smartbancs.consumidor_ia")


def trace_id_actual():
    contexto = trace.get_current_span().get_span_context()
    return format(contexto.trace_id, "032x") if contexto.is_valid else None


def crear_conexion_rabbit():
    credenciales = pika.PlainCredentials(
        RABBITMQ_USER,
        RABBITMQ_PASSWORD,
    )

    parametros = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        credentials=credenciales,
        heartbeat=60,
        blocked_connection_timeout=30,
    )

    return pika.BlockingConnection(
        parametros
    )


def construir_solicitud_ia(
    mensaje: dict,
) -> SolicitudAnalisis:

    tipo_evento = mensaje[
        "tipo_evento"
    ]

    if tipo_evento != "TRANSFERENCIA_COMPLETADA":
        raise ValueError(
            "Tipo de evento no soportado por IA: "
            f"{tipo_evento}"
        )

    datos = mensaje.get(
        "datos",
        {},
    )

    return SolicitudAnalisis(
        id_transaccion=mensaje[
            "id_transaccion"
        ],
        monto=datos["monto"],
        divisa=datos["divisa"],
        tipo="TRANSFERENCIA",
    )


def procesar_mensaje(
    mensaje: dict,
):
    solicitud = construir_solicitud_ia(
        mensaje
    )

    with tracer.start_as_current_span("ia.analizar_transaccion") as span:
        span.set_attribute("gen_ai.operation.name", "inference")
        inicio_ia = time.perf_counter()
        try:
            resultado = analizar_transaccion(solicitud)
        except Exception as exc:
            IA_ANALISIS_TOTAL.labels(resultado="error").inc()
            logger.error("Error de analisis IA", extra={
                "evento": "ia_analisis_error", "trace_id": trace_id_actual(),
                "id_transaccion": str(solicitud.id_transaccion),
                "tipo_error": type(exc).__name__,
            })
            raise
        finally:
            IA_DURACION_SECONDS.observe(time.perf_counter() - inicio_ia)
        IA_ANALISIS_TOTAL.labels(resultado="ok").inc()
        span.set_attribute("smartbancs.ai.model", resultado.modelo)
        span.set_attribute("smartbancs.ai.level", resultado.nivel)

    conn = None

    try:
        conn = crear_conexion()

        with tracer.start_as_current_span("db.guardar_analisis") as span:
            span.set_attribute("db.system.name", "postgresql")
            span.set_attribute("db.operation.name", "INSERT_OR_UPDATE")
            span.set_attribute("db.namespace", os.environ.get("DB_NAME", ""))
            inicio_persistencia = time.perf_counter()
            try:
                guardar_analisis(conn, resultado)
            except Exception as exc:
                logger.error("Error de persistencia IA", extra={
                    "evento": "ia_persistencia_error", "trace_id": trace_id_actual(),
                    "id_transaccion": str(resultado.id_transaccion),
                    "tipo_error": type(exc).__name__,
                })
                raise
            finally:
                IA_PERSISTENCIA_DURACION_SECONDS.observe(time.perf_counter() - inicio_persistencia)

    finally:
        if conn is not None:
            conn.close()

    logger.info("Analisis IA completado", extra={
        "evento": "ia_analisis_completado", "trace_id": trace_id_actual(),
        "id_transaccion": str(resultado.id_transaccion),
    })
    print(
        "[IA] Análisis completado "
        f"id_transaccion={resultado.id_transaccion} "
        f"nivel={resultado.nivel} "
        f"score={resultado.score} "
        f"modelo={resultado.modelo}"
    )

    return resultado


def callback(
    canal,
    metodo,
    properties,
    body,
):
    try:
        mensaje = json.loads(
            body.decode("utf-8")
        )

        contexto_padre = extract(mensaje.get("trace_context", {}))
        with tracer.start_as_current_span(
            "consumidor_ia.procesar", context=contexto_padre
        ) as span:
            span.set_attribute("messaging.system", "rabbitmq")
            span.set_attribute("messaging.destination.name", RABBITMQ_QUEUE_IA)
            span.set_attribute("messaging.operation.name", "process")
            span.set_attribute("smartbancs.event.type", mensaje["tipo_evento"])
            procesar_mensaje(mensaje)

        canal.basic_ack(
            delivery_tag=metodo.delivery_tag
        )
        IA_MENSAJES_TOTAL.labels(resultado="procesado").inc()

    except Exception as exc:
        print(
            "[IA] Error procesando mensaje: "
            f"{exc}"
        )

        canal.basic_nack(
            delivery_tag=metodo.delivery_tag,
            requeue=False,
        )
        IA_MENSAJES_TOTAL.labels(resultado="rechazado").inc()


def ejecutar_consumidor():
    print(
        "[IA] Consumidor iniciado"
    )

    while True:
        try:
            conexion = crear_conexion_rabbit()

            canal = conexion.channel()

            canal.queue_declare(
                queue=RABBITMQ_QUEUE_IA,
                durable=True,
            )

            canal.basic_qos(
                prefetch_count=1,
            )

            canal.basic_consume(
                queue=RABBITMQ_QUEUE_IA,
                on_message_callback=callback,
                auto_ack=False,
            )

            print(
                "[IA] Esperando mensajes..."
            )

            canal.start_consuming()

        except KeyboardInterrupt:
            break

        except Exception as exc:
            print(
                "[IA] Error de conexión: "
                f"{exc}"
            )

            time.sleep(5)


def main():
    configurar_logging()
    configurar_opentelemetry()
    start_http_server(int(os.getenv("METRICS_PORT", "9102")))
    ejecutar_consumidor()


if __name__ == "__main__":
    main()
