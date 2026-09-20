import json
import logging
import sys


class JsonFormatter(logging.Formatter):
    def format(self, record):
        datos = {"nivel": record.levelname, "mensaje": record.getMessage()}
        for campo in ("evento", "trace_id", "id_transaccion", "tipo_evento", "tipo_error"):
            valor = getattr(record, campo, None)
            if valor is not None:
                datos[campo] = valor
        return json.dumps(datos, default=str)


def configurar_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
