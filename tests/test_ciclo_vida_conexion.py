from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from psycopg2 import InterfaceError, OperationalError, errors
from psycopg2.pool import ThreadedConnectionPool

from servicio_transacciones.app.database import conexion
from servicio_transacciones.app.servicios import transferencias


class _QueryCanceled57014(errors.QueryCanceled):
    pgcode = "57014"


def _conn(cerrada=0):
    conn = MagicMock()
    conn.closed = cerrada
    return conn


def _prestar(monkeypatch, conn):
    pool = MagicMock()
    pool.getconn.return_value = conn
    monkeypatch.setattr(conexion, "_obtener_pool", lambda: pool)
    return pool


def _usar(gen, exc=None):
    next(gen)
    if exc is None:
        with pytest.raises(StopIteration):
            next(gen)
        return None
    with pytest.raises(type(exc)) as info:
        gen.throw(exc)
    return info.value


# --- obtener_conexion: descarte de conexiones rotas ----------------------------

@pytest.mark.parametrize("exc", [
    OperationalError("server closed the connection unexpectedly"),
    InterfaceError("connection already closed"),
])
def test_error_de_conexion_se_devuelve_con_close_true(monkeypatch, exc):
    conn = _conn()
    pool = _prestar(monkeypatch, conn)
    assert _usar(conexion.obtener_conexion(), exc) is exc  # original intacta
    conn.rollback.assert_not_called()  # no se revierte una conexion muerta
    pool.putconn.assert_called_once_with(conn, close=True)


def test_conexion_cerrada_se_descarta_sin_rollback(monkeypatch):
    conn = _conn(cerrada=2)
    pool = _prestar(monkeypatch, conn)
    _usar(conexion.obtener_conexion())
    conn.rollback.assert_not_called()
    pool.putconn.assert_called_once_with(conn, close=True)


def test_rollback_que_falla_descarta_y_no_tapa_la_excepcion_original(monkeypatch):
    conn = _conn()
    conn.rollback.side_effect = InterfaceError("connection already closed")
    pool = _prestar(monkeypatch, conn)
    original = ValueError("fallo original")
    assert _usar(conexion.obtener_conexion(), original) is original
    pool.putconn.assert_called_once_with(conn, close=True)


@pytest.mark.parametrize("exc", [
    _QueryCanceled57014(), errors.LockNotAvailable(), errors.DeadlockDetected(),
])
def test_conflictos_transitorios_no_descartan_conexion_sana(monkeypatch, exc):
    conn = _conn()
    pool = _prestar(monkeypatch, conn)
    _usar(conexion.obtener_conexion(), exc)
    conn.rollback.assert_called_once()
    pool.putconn.assert_called_once_with(conn, close=False)


def test_conexion_sana_se_reutiliza_normalmente(monkeypatch):
    conn = _conn()
    pool = _prestar(monkeypatch, conn)
    _usar(conexion.obtener_conexion())
    conn.rollback.assert_called_once()
    pool.putconn.assert_called_once_with(conn, close=False)


def test_pool_mantiene_capacidad_tras_descartar_conexion_rota(monkeypatch):
    def conectar(*a, **k):
        c = _conn()
        c.info.transaction_status = 0
        return c

    monkeypatch.setattr("psycopg2.connect", conectar)
    interno = ThreadedConnectionPool(1, 2)
    limitado = conexion.PoolConEspera(interno, 2)
    monkeypatch.setattr(conexion, "_obtener_pool", lambda: limitado)

    gen = conexion.obtener_conexion()
    rota = next(gen)
    with pytest.raises(OperationalError):
        gen.throw(OperationalError("server closed the connection unexpectedly"))
    rota.close.assert_called()  # cerrada por el pool
    assert limitado.slots._value == 2  # slot devuelto
    assert len(interno._used) == 0

    a, b = limitado.getconn(), limitado.getconn()  # sigue habiendo 2 conexiones
    assert rota not in (a, b)
    limitado.putconn(a)
    limitado.putconn(b)


# --- _MedirFinTransaccion / _procesar_transferencia ----------------------------

def test_exit_con_conexion_cerrada_no_ejecuta_conn_exit():
    conn = _conn(cerrada=2)
    original = OperationalError("server closed the connection unexpectedly")
    with pytest.raises(OperationalError) as info:
        with transferencias._MedirFinTransaccion(conn):
            raise original
    assert info.value is original
    conn.__exit__.assert_not_called()


@pytest.mark.parametrize("exc_rollback", [
    InterfaceError("connection already closed"),
    OperationalError("server closed the connection unexpectedly"),
])
def test_exit_no_tapa_la_excepcion_original_si_el_rollback_falla(exc_rollback):
    conn = _conn()
    conn.__exit__.side_effect = exc_rollback
    original = RuntimeError("original")
    with pytest.raises(RuntimeError) as info:
        with transferencias._MedirFinTransaccion(conn):
            raise original
    assert info.value is original  # sin doble excepcion


def test_exit_exitoso_sigue_haciendo_commit():
    conn = _conn()
    with transferencias._MedirFinTransaccion(conn):
        pass
    conn.__exit__.assert_called_once_with(None, None, None)


def test_commit_sobre_conexion_cerrada_no_se_oculta():
    conn = _conn(cerrada=2)
    conn.__exit__.side_effect = InterfaceError("connection already closed")
    with pytest.raises(InterfaceError):
        with transferencias._MedirFinTransaccion(conn):
            pass


@pytest.fixture
def flujo(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(transferencias, "obtener_o_crear_transaccion", lambda **k: {
        "creada": True, "transaccion": {"id_transaccion": "tx", "creado_en": "ahora"},
    })
    monkeypatch.setattr(transferencias, "debitar_cuenta", MagicMock(return_value=Decimal("90")))
    monkeypatch.setattr(transferencias, "acreditar_cuenta", MagicMock(return_value=Decimal("10")))
    for nombre in ("insertar_movimiento", "completar_transaccion", "insertar_evento_outbox"):
        monkeypatch.setattr(transferencias, nombre, MagicMock())
    return conn, (conn, "clave", "hash", "origen", "destino", Decimal("10.00"))


def _matar_en_sql(monkeypatch, conn, exc):
    def muere(*a, **k):
        conn.closed = 2  # psycopg2 marca la conexion como rota
        raise exc
    monkeypatch.setattr(transferencias, "debitar_cuenta", MagicMock(side_effect=muere))
    monkeypatch.setattr(transferencias, "acreditar_cuenta", MagicMock(side_effect=muere))


@pytest.mark.parametrize("exc", [
    OperationalError("server closed the connection unexpectedly"),
    InterfaceError("connection already closed"),
])
def test_conexion_muerta_durante_transaccion_propaga_original_sin_rollback(flujo, monkeypatch, exc):
    conn, args = flujo
    _matar_en_sql(monkeypatch, conn, exc)
    with pytest.raises(type(exc)) as info:
        transferencias._procesar_transferencia(*args)
    assert info.value is exc
    conn.rollback.assert_not_called()
    conn.__exit__.assert_not_called()


def test_rollback_fallido_en_conexion_abierta_no_tapa_la_excepcion(flujo, monkeypatch):
    conn, args = flujo
    original = RuntimeError("fallo")
    monkeypatch.setattr(transferencias, "debitar_cuenta", MagicMock(side_effect=original))
    monkeypatch.setattr(transferencias, "acreditar_cuenta", MagicMock(side_effect=original))
    conn.__exit__.side_effect = InterfaceError("connection already closed")
    conn.rollback.side_effect = InterfaceError("connection already closed")
    with pytest.raises(RuntimeError) as info:
        transferencias._procesar_transferencia(*args)
    assert info.value is original


def test_flujo_completo_conexion_rota_se_descarta_en_el_pool(flujo, monkeypatch):
    conn, args = flujo
    pool = _prestar(monkeypatch, conn)
    error = OperationalError("server closed the connection unexpectedly")
    _matar_en_sql(monkeypatch, conn, error)
    monkeypatch.setattr(transferencias, "obtener_sqlstate", lambda _: None)
    with pytest.raises(OperationalError) as info:
        transferencias.procesar_transferencia(
            id_idempotencia="clave", hash_solicitud="hash", cuenta_origen_id="origen",
            cuenta_destino_id="destino", monto=Decimal("10.00"),
        )
    assert info.value is error
    pool.putconn.assert_called_once_with(conn, close=True)
