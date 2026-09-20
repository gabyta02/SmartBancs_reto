import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):

    def format(self, record):
        log = {
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
            "nivel": record.levelname,
            "logger": record.name,
            "mensaje": record.getMessage(),
        }

        campos_extra = [
            "evento",
            "trace_id",
            "id_transaccion",
            "duracion_ms",
            "operacion",
            "estado",
            "error",
            "sqlstate",
            "tipo_error",
            "motivo",
        ]

        for campo in campos_extra:
            valor = getattr(
                record,
                campo,
                None,
            )

            if valor is not None:
                log[campo] = valor

        return json.dumps(
            log,
            default=str,
        )


def configurar_logging():
    handler = logging.StreamHandler(
        sys.stdout
    )

    handler.setFormatter(
        JsonFormatter()
    )

    root_logger = logging.getLogger()

    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
