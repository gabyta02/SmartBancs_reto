from worker_outbox.app.repositorios.outbox import (
    devolver_evento_a_pendiente,
    marcar_evento_fallido,
    marcar_evento_publicado,
    reclamar_eventos_pendientes,
)


MAX_INTENTOS_OUTBOX = 3


def reclamar_lote_outbox(
    conn,
    limite: int = 20,
):
    """
    Reclama un conjunto de eventos pendientes.

    La transacción termina inmediatamente después de marcar
    los eventos como PROCESANDO.
    """

    with conn:
        with conn.cursor() as cur:
            eventos = reclamar_eventos_pendientes(
                cur,
                limite=limite,
            )

    return eventos


def registrar_evento_publicado(
    conn,
    id_eventos,
):
    with conn:
        with conn.cursor() as cur:
            marcar_evento_publicado(
                cur,
                id_eventos,
            )


def registrar_fallo_procesamiento(
    conn,
    evento,
):
    """
    Decide si el evento vuelve a PENDIENTE o si ya debe
    marcarse definitivamente como FALLIDO.
    """

    intentos_actuales = evento["intentos"]

    with conn:
        with conn.cursor() as cur:

            if intentos_actuales + 1 >= MAX_INTENTOS_OUTBOX:
                marcar_evento_fallido(
                    cur,
                    evento["id_eventos"],
                )

                return "FALLIDO"

            devolver_evento_a_pendiente(
                cur,
                evento["id_eventos"],
            )

            return "PENDIENTE"