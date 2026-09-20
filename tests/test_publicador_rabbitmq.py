import json
from unittest.mock import MagicMock, patch

from worker_outbox.app.publicador import (
    construir_mensaje_ia,
    publicar_evento_ia,
)
from worker_outbox.app.configuracion import settings

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


def test_construir_mensaje_ia():
    evento = crear_evento_prueba()

    mensaje = construir_mensaje_ia(
        evento
    )

    assert mensaje == {
        "id_evento":
            "11111111-1111-1111-1111-111111111111",
        "id_transaccion":
            "22222222-2222-2222-2222-222222222222",
        "tipo_evento":
            "TRANSFERENCIA_COMPLETADA",
        "datos": {
            "monto": "1500.00",
            "divisa": "USD",
            "tipo": "TRANSFERENCIA",
        },
    }


@patch(
    "worker_outbox.app.publicador.pika.BasicProperties"
)
@patch(
    "worker_outbox.app.publicador.crear_conexion_rabbit"
)
def test_publicar_evento_ia(
    mock_crear_conexion,
    mock_basic_properties,
):
    evento = crear_evento_prueba()

    conexion = MagicMock()
    canal = MagicMock()

    conexion.channel.return_value = canal
    conexion.is_open = True

    mock_crear_conexion.return_value = conexion

    propiedades = MagicMock()
    mock_basic_properties.return_value = propiedades

    publicar_evento_ia(
        evento
    )

    mock_crear_conexion.assert_called_once()

    conexion.channel.assert_called_once()

    canal.queue_declare.assert_called_once_with(
        queue=settings.rabbitmq_queue_ia,
        durable=True,
    )

    canal.confirm_delivery.assert_called_once()

    mock_basic_properties.assert_called_once_with(
        delivery_mode=2,
        content_type="application/json",
    )

    canal.basic_publish.assert_called_once()

    argumentos = canal.basic_publish.call_args.kwargs

    assert argumentos["exchange"] == ""
    assert (argumentos["routing_key"] == settings.rabbitmq_queue_ia)
    assert argumentos["mandatory"] is True
    assert argumentos["properties"] == propiedades

    body = json.loads(
        argumentos["body"]
    )

    assert body["id_evento"] == (
        "11111111-1111-1111-1111-111111111111"
    )

    assert body["id_transaccion"] == (
        "22222222-2222-2222-2222-222222222222"
    )

    assert (
        body["tipo_evento"]
        == "TRANSFERENCIA_COMPLETADA"
    )

    assert body["datos"]["monto"] == "1500.00"
    assert body["datos"]["divisa"] == "USD"

    conexion.close.assert_called_once()

@patch(
    "worker_outbox.app.publicador.crear_conexion_rabbit"
)
def test_publicar_evento_ia_propaga_error(
    mock_crear_conexion,
):
    evento = crear_evento_prueba()

    conexion = MagicMock()
    canal = MagicMock()

    conexion.channel.return_value = canal
    conexion.is_open = True

    mock_crear_conexion.return_value = conexion

    canal.basic_publish.side_effect = RuntimeError(
        "RabbitMQ no disponible"
    )

    try:
        publicar_evento_ia(
            evento
        )

        assert False, (
            "Se esperaba RuntimeError"
        )

    except RuntimeError as exc:
        assert (
            str(exc)
            == "RabbitMQ no disponible"
        )

    conexion.close.assert_called_once()
