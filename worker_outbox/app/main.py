import time
import logging
import os
from prometheus_client import start_http_server
from collections.abc import Callable
from typing import Any

from worker_outbox.app.observabilidad.opentelemetry import configurar_opentelemetry
from worker_outbox.app.observabilidad.metricas import OUTBOX_EVENTOS_TOTAL
from worker_outbox.app.observabilidad.logging import configurar_logging

from worker_outbox.app.database.conexion import (
    crear_conexion,
)

from worker_outbox.app.servicios.outbox import (
    reclamar_lote_outbox,
    registrar_evento_publicado,
    registrar_fallo_procesamiento,
)

from worker_outbox.app.servicios.procesador_eventos import (
    procesar_evento_outbox,
)

logger = logging.getLogger("smartbancs.worker_outbox")


def procesar_lote_outbox(
    conn,
    procesador_evento: Callable[
        [dict[str, Any]],
        None,
    ],
    limite: int = 20,
) -> dict:

    eventos = reclamar_lote_outbox(
        conn,
        limite=limite,
    )

    resultado = {
        "reclamados": len(eventos),
        "publicados": 0,
        "reintentados": 0,
        "fallidos": 0,
    }

    for evento in eventos:
        try:
            procesador_evento(evento)

            registrar_evento_publicado(
                conn,
                evento["id_eventos"],
            )

            resultado["publicados"] += 1
            OUTBOX_EVENTOS_TOTAL.labels(resultado="publicado").inc()
            logger.info("Evento outbox publicado", extra={
                "evento": "outbox_publicado", "id_transaccion": str(evento["transaccion_id"]),
                "trace_id": evento.get("carga_util", {}).get("trace_id"),
            })

        except Exception as exc:
            print(
                "[OUTBOX] Error procesando "
                f"{evento['id_eventos']}: {exc}"
            )

            estado = registrar_fallo_procesamiento(
                conn,
                evento,
            )

            if estado == "FALLIDO":
                resultado["fallidos"] += 1
                OUTBOX_EVENTOS_TOTAL.labels(resultado="fallido").inc()
                logger.error("Evento outbox fallido", extra={
                    "evento": "outbox_fallido", "id_transaccion": str(evento["transaccion_id"]),
                    "tipo_error": type(exc).__name__,
                    "trace_id": evento.get("carga_util", {}).get("trace_id"),
                })
            else:
                resultado["reintentados"] += 1
                OUTBOX_EVENTOS_TOTAL.labels(resultado="reintento").inc()
                logger.warning("Evento outbox para reintento", extra={
                    "evento": "outbox_reintento", "id_transaccion": str(evento["transaccion_id"]),
                    "tipo_error": type(exc).__name__,
                    "trace_id": evento.get("carga_util", {}).get("trace_id"),
                })

    return resultado


def ejecutar_worker(
    limite: int = 20,
    intervalo_segundos: float = 1.0,
) -> None:

    print("[OUTBOX] Worker iniciado")

    while True:
        conn = None
        resultado = {
            "reclamados": 0,
        }

        try:
            conn = crear_conexion()

            resultado = procesar_lote_outbox(
                conn=conn,
                procesador_evento=procesar_evento_outbox,
                limite=limite,
            )

            if resultado["reclamados"] > 0:
                print(
                    "[OUTBOX] "
                    f"reclamados="
                    f"{resultado['reclamados']} "
                    f"publicados="
                    f"{resultado['publicados']} "
                    f"reintentados="
                    f"{resultado['reintentados']} "
                    f"fallidos="
                    f"{resultado['fallidos']}"
                )

        except Exception as exc:
            print(
                "[OUTBOX] Error general: "
                f"{exc}"
            )

        finally:
            if conn is not None:
                conn.close()

        if resultado["reclamados"] == 0:
            time.sleep(intervalo_segundos)


def main():
    configurar_logging()
    configurar_opentelemetry()
    start_http_server(int(os.getenv("METRICS_PORT", "9101")))
    ejecutar_worker(
        limite=20,
        intervalo_segundos=1.0,
    )


if __name__ == "__main__":
    main()
