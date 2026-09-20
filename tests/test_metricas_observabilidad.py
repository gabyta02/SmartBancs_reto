from unittest.mock import MagicMock

import pytest

from servicio_transacciones.app.servicios import transferencias
from worker_outbox.app import publicador
from servicio_ia.app import consumidor


def test_transferencia_rechazada_y_reproducida(monkeypatch):
    from decimal import Decimal

    conn = MagicMock()
    conn.closed = 0
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {
        "id_transaccion": "tx", "id_idempotencia": "clave",
        "cuenta_origen_id": "origen", "cuenta_destino_id": "destino",
        "monto": Decimal("10.00"), "divisa": "USD", "estado": "RECHAZADA",
        "razon_fallo": "SALDO_INSUFICIENTE", "creado_en": "ahora",
    }
    total = MagicMock()
    errores = MagicMock()
    reproducidas = MagicMock()
    duracion = MagicMock()
    monkeypatch.setattr(transferencias, "TRANSACCIONES_TOTAL", total)
    monkeypatch.setattr(transferencias, "ERRORES_TRANSACCIONES_TOTAL", errores)
    monkeypatch.setattr(transferencias, "TRANSACCIONES_REPRODUCIDAS_TOTAL", reproducidas)
    monkeypatch.setattr(transferencias, "DURACION_TRANSFERENCIA", duracion)
    monkeypatch.setattr(transferencias, "rechazar_transaccion", lambda *a: None)
    monkeypatch.setattr(transferencias, "obtener_cuentas_para_actualizar", lambda *a: {
        "origen": {"estado": "ACTIVA", "divisa": "USD", "saldo": Decimal("1.00")},
        "destino": {"estado": "ACTIVA", "divisa": "USD", "saldo": Decimal("0.00")},
    })
    monkeypatch.setattr(transferencias, "obtener_o_crear_transaccion", lambda **k: {
        "creada": True, "transaccion": {"id_transaccion": "tx", "creado_en": "ahora"},
    })
    monkeypatch.setattr(transferencias, "acreditar_cuenta", lambda *a: Decimal("10.00"))
    monkeypatch.setattr(transferencias, "debitar_cuenta", lambda *a: None)
    argumentos = (conn, "clave", "hash", "origen", "destino", Decimal("10.00"))
    assert transferencias.procesar_transferencia(*argumentos)["estado"] == "RECHAZADA"
    total.labels.assert_called_with(estado="RECHAZADA")
    assert duracion.observe.call_count == 1
    errores.labels.assert_not_called()

    monkeypatch.setattr(transferencias, "obtener_o_crear_transaccion", lambda **k: {
        "creada": False, "transaccion": cur.fetchone(),
    })
    assert transferencias.procesar_transferencia(*argumentos)["reproducida"] is True
    reproducidas.inc.assert_called_once()


def test_sqlstate_y_clasificacion_db(monkeypatch):
    deadlocks = MagicMock()
    timeouts = MagicMock()
    errores = MagicMock()
    monkeypatch.setattr(transferencias, "DB_DEADLOCKS_TOTAL", deadlocks)
    monkeypatch.setattr(transferencias, "DB_TIMEOUTS_TOTAL", timeouts)
    monkeypatch.setattr(transferencias, "ERRORES_TRANSACCIONES_TOTAL", errores)

    for codigo, tipo in (("40P01", "deadlock"), ("57014", "timeout"),
                         ("55P03", "timeout"), ("23505", "db_error")):
        exc = Exception("dato privado")
        exc.pgcode = codigo
        assert transferencias.obtener_sqlstate(exc) == codigo
        transferencias._registrar_error_bd(exc, "abc")
        errores.labels.assert_any_call(tipo=tipo)

    assert deadlocks.inc.call_count == 1
    assert timeouts.inc.call_count == 2
    assert errores.labels.call_count == 4


def test_publicacion_registra_resultado(monkeypatch):
    conexion = MagicMock()
    conexion.is_open = True
    monkeypatch.setattr(publicador, "crear_conexion_rabbit", lambda: conexion)
    contador = MagicMock()
    duracion = MagicMock()
    monkeypatch.setattr(publicador, "RABBITMQ_PUBLICACIONES_TOTAL", contador)
    monkeypatch.setattr(publicador, "RABBITMQ_PUBLICACION_DURACION_SECONDS", duracion)
    evento = {"id_eventos": "1", "transaccion_id": "2",
              "tipo_evento": "TRANSFERENCIA_COMPLETADA", "carga_util": {}}

    publicador.publicar_evento_ia(evento)
    contador.labels.assert_called_with(resultado="ok")
    assert duracion.observe.call_count == 1

    conexion.channel.return_value.basic_publish.side_effect = RuntimeError("fallo")
    with pytest.raises(RuntimeError):
        publicador.publicar_evento_ia(evento)
    contador.labels.assert_called_with(resultado="error")
    assert duracion.observe.call_count == 2


def test_ia_registra_analisis_y_persistencia(monkeypatch):
    contador = MagicMock()
    duracion = MagicMock()
    persistencia = MagicMock()
    monkeypatch.setattr(consumidor, "IA_ANALISIS_TOTAL", contador)
    monkeypatch.setattr(consumidor, "IA_DURACION_SECONDS", duracion)
    monkeypatch.setattr(consumidor, "IA_PERSISTENCIA_DURACION_SECONDS", persistencia)
    monkeypatch.setattr(consumidor, "crear_conexion", MagicMock())
    monkeypatch.setattr(consumidor, "guardar_analisis", MagicMock())
    mensaje = {"tipo_evento": "TRANSFERENCIA_COMPLETADA",
               "id_transaccion": "11111111-1111-1111-1111-111111111111",
               "datos": {"monto": "10.00", "divisa": "USD"}}

    consumidor.procesar_mensaje(mensaje)
    contador.labels.assert_called_with(resultado="ok")
    assert duracion.observe.call_count == 1
    assert persistencia.observe.call_count == 1

    monkeypatch.setattr(consumidor, "analizar_transaccion", MagicMock(side_effect=RuntimeError("fallo")))
    with pytest.raises(RuntimeError):
        consumidor.procesar_mensaje(mensaje)
    contador.labels.assert_called_with(resultado="error")
    assert duracion.observe.call_count == 2

def test_lifespan_configura_40_tokens_anyio():
    from fastapi.testclient import TestClient
    from servicio_transacciones.app.configuracion import settings
    from servicio_transacciones.app.main import app
    from servicio_transacciones.app.observabilidad.metricas import (
        THREADPOOL_TOKENS_TOTAL,
        worker_actual,
    )

    assert settings.anyio_thread_tokens == 40
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert THREADPOOL_TOKENS_TOTAL.labels(
            worker=worker_actual()
        )._value.get() == 40
