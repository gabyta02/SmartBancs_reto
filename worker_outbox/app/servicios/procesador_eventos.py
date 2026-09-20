from typing import Any

from worker_outbox.app.publicador import (
    publicar_evento_ia,
)


def procesar_evento_outbox(
    evento: dict[str, Any],
) -> None:

    tipo_evento = evento[
        "tipo_evento"
    ]

    if tipo_evento == "TRANSFERENCIA_COMPLETADA":
        publicar_evento_ia(
            evento
        )
        return

    raise ValueError(
        "Tipo de evento no soportado: "
        f"{tipo_evento}"
    )