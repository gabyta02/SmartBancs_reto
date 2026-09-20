import os
import threading
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from psycopg2 import errors
from psycopg2.pool import PoolError

from servicio_transacciones.app.database import conexion
from servicio_transacciones.app.main import app
from servicio_transacciones.app.rutas import transferencias as rutas
from servicio_transacciones.app.servicios import transferencias

DETALLE_503 = "Servicio temporalmente saturado. Reintente con la misma clave de idempotencia."
PAYLOAD = {
    "id_idempotencia": "clave-errores-http",
    "cuenta_origen_id": "11111111-1111-1111-1111-111111111111",
    "cuenta_destino_id": "22222222-2222-2222-2222-222222222222",
    "monto": "10.00", "divisa": "USD",
}


def _valor(status, tipo, motivo):
    return REGISTRY.get_sample_value("smartbancs_http_errores_total", {
        "worker": str(os.getpid()), "endpoint": "/transacciones",
        "status": str(status), "tipo": tipo, "motivo": motivo,
    }) or 0.0


@pytest.fixture
def cliente():
    return TestClient(app, raise_server_exceptions=False)


def _pool_que_falla(exc):
    def prestar():
        raise exc
        yield
    return prestar


def _sin_conexion(monkeypatch, exc_intento):
    """Conexion simulada; `_procesar_transferencia` lanza `exc_intento`."""
    monkeypatch.setattr(transferencias, "obtener_conexion", _gen)
    monkeypatch.setattr(transferencias, "_procesar_transferencia",
                        MagicMock(side_effect=exc_intento))
    monkeypatch.setattr(transferencias.time, "sleep", lambda _: None)


def _gen():
    yield MagicMock()


# --- Pool: la excepcion conserva el motivo -------------------------------------

def test_pool_agotado_conserva_motivo_timeout(monkeypatch):
    monkeypatch.setattr(conexion.settings, "db_pool_timeout_s", 0.01)
    limitado = conexion.PoolConEspera(MagicMock(), capacidad=1)
    limitado.getconn()
    with pytest.raises(conexion.PoolAgotado) as info:
        limitado.getconn()
    assert info.value.motivo == "timeout"


def test_pool_agotado_conserva_motivo_cola_llena(monkeypatch):
    limitado = conexion.PoolConEspera(MagicMock(), capacidad=1)
    limitado.cola = threading.BoundedSemaphore(1)
    limitado.cola.acquire()  # cola ya ocupada
    limitado.getconn()
    with pytest.raises(conexion.PoolAgotado) as info:
        limitado.getconn()
    assert info.value.motivo == "cola_llena"


def test_pool_agotado_conserva_motivo_pool_error():
    interno = MagicMock()
    interno.getconn.side_effect = PoolError("agotado")
    with pytest.raises(conexion.PoolAgotado) as info:
        conexion.PoolConEspera(interno, capacidad=1).getconn()
    assert info.value.motivo == "pool_error"


# --- HTTP: contadores y contrato publico ---------------------------------------

@pytest.mark.parametrize("motivo", ["timeout", "cola_llena", "pool_error"])
def test_503_pool_incrementa_motivo_y_mantiene_contrato(monkeypatch, cliente, motivo):
    monkeypatch.setattr(transferencias, "obtener_conexion",
                        _pool_que_falla(conexion.PoolAgotado("x", motivo=motivo)))
    antes = _valor(503, "pool", motivo)
    r = cliente.post("/transacciones", json=PAYLOAD)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "1"
    assert r.json() == {"detail": DETALLE_503}
    assert _valor(503, "pool", motivo) == antes + 1


@pytest.mark.parametrize("exc,codigo", [
    (errors.QueryCanceled, "57014"),
    (errors.LockNotAvailable, "55P03"),
    (errors.DeadlockDetected, "40P01"),
])
def test_503_servicio_saturado_incrementa_sqlstate(monkeypatch, cliente, exc, codigo):
    _sin_conexion(monkeypatch, exc())
    monkeypatch.setattr(transferencias, "obtener_sqlstate", lambda _: codigo)
    antes = _valor(503, "servicio_saturado", codigo)
    r = cliente.post("/transacciones", json=PAYLOAD)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "1"
    assert r.json() == {"detail": DETALLE_503}
    assert _valor(503, "servicio_saturado", codigo) == antes + 1  # una vez por peticion


def test_500_db_error_no_reintentable(monkeypatch, cliente):
    _sin_conexion(monkeypatch, errors.CheckViolation())
    monkeypatch.setattr(transferencias, "obtener_sqlstate", lambda _: "23514")
    antes_db = _valor(500, "db_error", "23514")
    antes_interno = _valor(500, "interno", "excepcion_no_controlada")
    r = cliente.post("/transacciones", json=PAYLOAD)
    assert r.status_code == 500
    assert _valor(500, "db_error", "23514") == antes_db + 1
    assert _valor(500, "interno", "excepcion_no_controlada") == antes_interno  # sin doble conteo


def test_500_excepcion_no_controlada(monkeypatch, cliente):
    _sin_conexion(monkeypatch, RuntimeError("inesperado"))
    antes = _valor(500, "interno", "excepcion_no_controlada")
    r = cliente.post("/transacciones", json=PAYLOAD)
    assert r.status_code == 500
    assert _valor(500, "interno", "excepcion_no_controlada") == antes + 1


def test_404_cuenta_no_encontrada(monkeypatch, cliente):
    monkeypatch.setattr(rutas, "procesar_transferencia",
                        MagicMock(side_effect=transferencias.CuentaNoEncontrada("x")))
    antes = _valor(404, "negocio", "cuenta_no_encontrada")
    assert cliente.post("/transacciones", json=PAYLOAD).status_code == 404
    assert _valor(404, "negocio", "cuenta_no_encontrada") == antes + 1


def test_409_conflicto_idempotencia(monkeypatch, cliente):
    monkeypatch.setattr(rutas, "procesar_transferencia",
                        MagicMock(side_effect=rutas.ConflictoIdempotencia("x")))
    antes = _valor(409, "negocio", "conflicto_idempotencia")
    assert cliente.post("/transacciones", json=PAYLOAD).status_code == 409
    assert _valor(409, "negocio", "conflicto_idempotencia") == antes + 1


def test_422_validacion(cliente):
    antes = _valor(422, "validacion", "validacion")
    r = cliente.post("/transacciones", json={**PAYLOAD, "monto": "-1"})
    assert r.status_code == 422
    assert _valor(422, "validacion", "validacion") == antes + 1


def test_exito_no_incrementa_errores(monkeypatch, cliente):
    from datetime import datetime, timezone
    from uuid import UUID
    monkeypatch.setattr(rutas, "procesar_transferencia", lambda **k: {
        "id_transaccion": UUID("33333333-3333-3333-3333-333333333333"),
        "id_idempotencia": k["id_idempotencia"],
        "cuenta_origen_id": k["cuenta_origen_id"],
        "cuenta_destino_id": k["cuenta_destino_id"],
        "monto": k["monto"], "divisa": k["divisa"], "estado": "COMPLETADA",
        "razon_fallo": None, "creado_en": datetime.now(timezone.utc), "reproducida": False,
    })
    antes = {s.labels["status"] + s.labels["tipo"] for s in _muestras()}
    assert cliente.post("/transacciones", json=PAYLOAD).status_code == 201
    assert {s.labels["status"] + s.labels["tipo"] for s in _muestras()} == antes


def _muestras():
    return [s for m in REGISTRY.collect() if m.name == "smartbancs_http_errores"
            for s in m.samples if s.name == "smartbancs_http_errores_total"]
