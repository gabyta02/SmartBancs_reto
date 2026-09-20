import pytest

from worker_outbox.app.servicios.procesador_eventos import (
    procesar_evento_outbox,
)

from unittest.mock import patch

from worker_outbox.app.servicios.procesador_eventos import (
    procesar_evento_outbox,
)


def crear_evento_prueba():
    return {
        "id_eventos": "11111111-1111-1111-1111-111111111111",
        "transaccion_id": "22222222-2222-2222-2222-222222222222",
        "tipo_evento": "TRANSFERENCIA_COMPLETADA",
        "carga_util": {
            "monto": "1500.00",
            "divisa": "USD",
            "tipo": "TRANSFERENCIA",
        },
    }


@patch(
    "worker_outbox.app.servicios.procesador_eventos.publicar_evento_ia"
)
def test_procesar_evento_publica_en_rabbit(
    mock_publicar,
):
    evento = crear_evento_prueba()

    procesar_evento_outbox(evento)

    mock_publicar.assert_called_once_with(evento)


def test_procesar_evento_tipo_no_soportado():
    evento = {
        "id_eventos": "11111111-1111-1111-1111-111111111111",
        "transaccion_id": "22222222-2222-2222-2222-222222222222",
        "tipo_evento": "EVENTO_DESCONOCIDO",
        "carga_util": {},
    }

    with pytest.raises(ValueError):
        procesar_evento_outbox(evento)
