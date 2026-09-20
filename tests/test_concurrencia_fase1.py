from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from psycopg2 import errors
from psycopg2.pool import PoolError

from servicio_transacciones.app.database import conexion
from servicio_transacciones.app.servicios import transferencias


@pytest.fixture
def flujo(monkeypatch):
    conn = MagicMock()
    conn.closed = 0  # abierta: en un MagicMock seria truthy, es decir "cerrada"
    cur = conn.cursor.return_value.__enter__.return_value
    fila = {
        "id_transaccion": "tx", "id_idempotencia": "clave",
        "cuenta_origen_id": "origen", "cuenta_destino_id": "destino",
        "monto": Decimal("10.00"), "divisa": "USD", "estado": "COMPLETADA",
        "razon_fallo": None, "creado_en": "ahora",
    }
    monkeypatch.setattr(transferencias, "obtener_o_crear_transaccion", lambda **k: {
        "creada": True, "transaccion": {"id_transaccion": "tx", "creado_en": "ahora"},
    })
    monkeypatch.setattr(transferencias, "obtener_cuentas_para_actualizar", lambda *a: {
        "origen": {"estado": "ACTIVA", "divisa": "USD", "saldo": Decimal("100.00")},
        "destino": {"estado": "ACTIVA", "divisa": "USD", "saldo": Decimal("0.00")},
    })
    debit = MagicMock(return_value=Decimal("90.00"))
    credit = MagicMock(return_value=Decimal("10.00"))
    monkeypatch.setattr(transferencias, "debitar_cuenta", debit)
    monkeypatch.setattr(transferencias, "acreditar_cuenta", credit)
    for nombre in ("insertar_movimiento", "completar_transaccion",
                   "insertar_evento_outbox", "rechazar_transaccion"):
        monkeypatch.setattr(transferencias, nombre, MagicMock())
    args = (conn, "clave", "hash", "origen", "destino", Decimal("10.00"))
    return conn, fila, args, debit, credit


def test_transferencia_normal_commit_antes_de_metricas(flujo, monkeypatch):
    conn, fila, args, debit, credit = flujo
    eventos = []
    conn.__exit__.side_effect = lambda *a: eventos.append("commit")
    metrica = MagicMock()
    metrica.labels.return_value.inc.side_effect = lambda: eventos.append("metrica")
    monkeypatch.setattr(transferencias, "TRANSACCIONES_TOTAL", metrica)
    assert transferencias.procesar_transferencia(*args)["estado"] == "COMPLETADA"
    assert eventos == ["commit", "metrica"]
    debit.assert_called_once()
    credit.assert_called_once()


def test_saldo_insuficiente_no_modifica_saldos(flujo, monkeypatch):
    conn, fila, args, debit, credit = flujo
    fila["estado"] = "RECHAZADA"
    fila["razon_fallo"] = "SALDO_INSUFICIENTE"
    debit.return_value = None
    monkeypatch.setattr(transferencias, "obtener_cuentas_para_actualizar", lambda *a: {
        "origen": {"estado": "ACTIVA", "divisa": "USD", "saldo": Decimal("1.00")},
        "destino": {"estado": "ACTIVA", "divisa": "USD", "saldo": Decimal("0.00")},
    })
    assert transferencias.procesar_transferencia(*args)["estado"] == "RECHAZADA"
    debit.assert_called_once()
    credit.assert_called_once()  # destino tiene el UUID menor en este fixture
    cur = conn.cursor.return_value.__enter__.return_value
    assert any("ROLLBACK TO SAVEPOINT sp_transferencia_saldos" in call.args[0]
               for call in cur.execute.call_args_list)
    transferencias.insertar_movimiento.assert_not_called()
    transferencias.insertar_evento_outbox.assert_not_called()
    transferencias.rechazar_transaccion.assert_called_once()


def test_excepcion_hace_rollback(flujo, monkeypatch):
    conn, fila, args, debit, credit = flujo
    debit.side_effect = RuntimeError("fallo")
    with pytest.raises(RuntimeError):
        transferencias.procesar_transferencia(*args)
    conn.rollback.assert_called_once()


@pytest.mark.parametrize("exc,codigo", [
    (errors.QueryCanceled, "57014"),
    (errors.DeadlockDetected, "40P01"),
    (errors.LockNotAvailable, "55P03"),
])
def test_retry_completo_misma_idempotencia(monkeypatch, flujo, exc, codigo):
    conn, fila, args, debit, credit = flujo
    original = transferencias._procesar_transferencia
    llamadas = []
    def intento(*a, **k):
        llamadas.append(a)
        if len(llamadas) == 1:
            raise exc()
        return original(*a, **k)
    monkeypatch.setattr(transferencias, "_procesar_transferencia", intento)
    monkeypatch.setattr(transferencias, "obtener_sqlstate", lambda _: codigo)
    monkeypatch.setattr(transferencias.time, "sleep", lambda _: None)
    assert transferencias.procesar_transferencia(*args)["estado"] == "COMPLETADA"
    assert len(llamadas) == 2
    assert llamadas[0] == llamadas[1]
    assert llamadas[0][1] == "clave"


def test_retries_agotados(monkeypatch, flujo):
    conn, fila, args, debit, credit = flujo
    monkeypatch.setattr(transferencias, "_procesar_transferencia",
                        MagicMock(side_effect=errors.QueryCanceled()))
    monkeypatch.setattr(transferencias, "obtener_sqlstate", lambda _: "57014")
    monkeypatch.setattr(transferencias.time, "sleep", lambda _: None)
    with pytest.raises(transferencias.ServicioSaturado):
        transferencias.procesar_transferencia(*args)
    assert transferencias._procesar_transferencia.call_count == min(
        transferencias.settings.max_reintentos, 3
    )


def test_pool_devuelve_conexion_y_rollback(monkeypatch):
    pool = MagicMock()
    conn = MagicMock()
    conn.closed = 0
    pool.getconn.return_value = conn
    monkeypatch.setattr(conexion, "_obtener_pool", lambda: pool)
    gen = conexion.obtener_conexion()
    assert next(gen) is conn
    with pytest.raises(StopIteration):
        next(gen)
    conn.rollback.assert_called_once()
    pool.putconn.assert_called_once_with(conn, close=False)


def test_pool_devuelve_conexion_tras_error_y_descarta_rota(monkeypatch):
    pool = MagicMock()
    conn = MagicMock()
    conn.closed = 0
    pool.getconn.return_value = conn
    monkeypatch.setattr(conexion, "_obtener_pool", lambda: pool)
    gen = conexion.obtener_conexion()
    next(gen)
    with pytest.raises(ValueError):
        gen.throw(ValueError("fallo"))
    pool.putconn.assert_called_once_with(conn, close=False)
    pool.reset_mock()
    conn.closed = 1
    gen = conexion.obtener_conexion()
    next(gen)
    gen.close()
    pool.putconn.assert_called_once_with(conn, close=True)


def test_pool_agotado(monkeypatch):
    pool = MagicMock()
    pool.getconn.side_effect = PoolError("agotado")
    monkeypatch.setattr(conexion, "_obtener_pool", lambda: conexion.PoolConEspera(pool, 1))
    with pytest.raises(conexion.PoolAgotado):
        next(conexion.obtener_conexion())

def test_timeout_en_update_reinicia_y_revierte(monkeypatch, flujo):
    conn, fila, args, debit, credit = flujo
    cuentas = {
        "origen": {"estado": "ACTIVA", "divisa": "USD", "saldo": Decimal("100.00")},
        "destino": {"estado": "ACTIVA", "divisa": "USD", "saldo": Decimal("0.00")},
    }
    debit.side_effect = [errors.QueryCanceled(), Decimal("90.00")]
    idem = MagicMock(return_value={
        "creada": True, "transaccion": {"id_transaccion": "tx", "creado_en": "ahora"},
    })
    bloqueo = MagicMock(return_value=cuentas)
    monkeypatch.setattr(transferencias, "obtener_cuentas_para_actualizar", bloqueo)
    monkeypatch.setattr(transferencias, "obtener_o_crear_transaccion", idem)
    monkeypatch.setattr(transferencias, "obtener_sqlstate", lambda _: "57014")
    monkeypatch.setattr(transferencias.time, "sleep", lambda _: None)
    assert transferencias.procesar_transferencia(*args)["estado"] == "COMPLETADA"
    assert conn.rollback.call_count >= 1
    assert idem.call_count == 2
    assert idem.call_args_list[0].kwargs["id_idempotencia"] == "clave"
    assert idem.call_args_list[1].kwargs["id_idempotencia"] == "clave"
    bloqueo.assert_not_called()


def test_endpoint_retries_agotados_devuelve_503(monkeypatch):
    from fastapi.testclient import TestClient
    from servicio_transacciones.app.main import app
    from servicio_transacciones.app.rutas import transferencias as rutas

    monkeypatch.setattr(
        rutas, "procesar_transferencia",
        MagicMock(side_effect=transferencias.ServicioSaturado("57014")),
    )
    respuesta = TestClient(app).post("/transacciones", json={
        "id_idempotencia": "clave",
        "cuenta_origen_id": "11111111-1111-1111-1111-111111111111",
        "cuenta_destino_id": "22222222-2222-2222-2222-222222222222",
        "monto": "10.00", "divisa": "USD",
    })
    assert respuesta.status_code == 503
    assert respuesta.headers["retry-after"] == "1"

def test_commit_fallido_no_registra_exito(flujo, monkeypatch):
    conn, fila, args, debit, credit = flujo
    conn.__exit__.side_effect = errors.QueryCanceled()
    metrica = MagicMock()
    monkeypatch.setattr(transferencias, "TRANSACCIONES_TOTAL", metrica)
    monkeypatch.setattr(transferencias, "obtener_sqlstate", lambda _: "57014")
    monkeypatch.setattr(transferencias.time, "sleep", lambda _: None)
    with pytest.raises(transferencias.ServicioSaturado):
        transferencias.procesar_transferencia(*args)
    metrica.labels.assert_not_called()
    assert conn.rollback.call_count >= 1

def test_ruta_exitosa_no_bloquea_y_actualiza_en_orden(flujo, monkeypatch):
    conn, fila, args, debit, credit = flujo
    orden = []
    debit.side_effect = lambda *a: orden.append("origen") or Decimal("90.00")
    credit.side_effect = lambda *a: orden.append("destino") or Decimal("10.00")
    bloqueo = MagicMock()
    monkeypatch.setattr(transferencias, "obtener_cuentas_para_actualizar", bloqueo)
    transferencias.procesar_transferencia(*args)
    assert orden == ["destino", "origen"]
    bloqueo.assert_not_called()
    cur = conn.cursor.return_value.__enter__.return_value
    sql = [call.args[0] for call in cur.execute.call_args_list]
    assert "SAVEPOINT sp_transferencia_saldos" in sql
    assert "RELEASE SAVEPOINT sp_transferencia_saldos" in sql
    assert not any("ROLLBACK TO SAVEPOINT sp_transferencia_saldos" in s for s in sql)


def test_fallo_despues_del_primer_update_revierte_todo(flujo):
    conn, fila, args, debit, credit = flujo
    debit.side_effect = RuntimeError("fallo despues del credito")
    with pytest.raises(RuntimeError):
        transferencias.procesar_transferencia(*args)
    credit.assert_called_once()
    conn.rollback.assert_called_once()
    transferencias.insertar_movimiento.assert_not_called()
    transferencias.insertar_evento_outbox.assert_not_called()


def test_fallback_clasifica_cambio_estado_y_divisa(flujo, monkeypatch):
    conn, fila, args, debit, credit = flujo
    debit.return_value = None
    fila["estado"] = "RECHAZADA"
    casos = [
        ({"estado": "BLOQUEADA", "divisa": "USD", "saldo": Decimal("0")},
         "CUENTA_DESTINO_NO_ACTIVA"),
        ({"estado": "ACTIVA", "divisa": "EUR", "saldo": Decimal("0")},
         "DIVISA_NO_COMPATIBLE"),
    ]
    for destino, razon in casos:
        fila["razon_fallo"] = razon
        bloqueo = MagicMock(return_value={
            "origen": {"estado": "ACTIVA", "divisa": "USD", "saldo": Decimal("100")},
            "destino": destino,
        })
        monkeypatch.setattr(transferencias, "obtener_cuentas_para_actualizar", bloqueo)
        assert transferencias.procesar_transferencia(*args)["razon_fallo"] == razon
        bloqueo.assert_called_once()
    transferencias.insertar_movimiento.assert_not_called()
    transferencias.insertar_evento_outbox.assert_not_called()


def test_sql_updates_condicionados():
    from servicio_transacciones.app.repositorios.transacciones import (
        debitar_cuenta, acreditar_cuenta,
    )
    cur = MagicMock()
    cur.fetchone.side_effect = [(Decimal("90.00"),), (Decimal("10.00"),)]
    assert debitar_cuenta(cur, "origen", Decimal("10"), "USD") == Decimal("90.00")
    assert acreditar_cuenta(cur, "destino", Decimal("10"), "USD") == Decimal("10.00")
    debit_sql, debit_args = cur.execute.call_args_list[0].args
    credit_sql, credit_args = cur.execute.call_args_list[1].args
    assert "saldo >= %s" in debit_sql
    assert "estado = 'ACTIVA'" in debit_sql and "divisa = %s" in debit_sql
    assert "estado = 'ACTIVA'" in credit_sql and "divisa = %s" in credit_sql
    assert debit_args == (Decimal("10"), "origen", "USD", Decimal("10"))
    assert credit_args == (Decimal("10"), "destino", "USD")

def test_retry_libera_conexion_antes_de_dormir_y_toma_otra(monkeypatch):
    eventos = []
    conexiones = [MagicMock(name="primera"), MagicMock(name="segunda")]

    def prestar():
        conn = conexiones.pop(0)
        eventos.append(("adquirir", conn))
        try:
            yield conn
        finally:
            eventos.append(("devolver", conn))

    llamadas = []
    def intento(conn, id_idempotencia):
        llamadas.append((conn, id_idempotencia))
        if len(llamadas) == 1:
            raise errors.QueryCanceled()
        return {
            "id_transaccion": "tx", "estado": "COMPLETADA",
            "razon_fallo": None, "reproducida": False,
        }

    monkeypatch.setattr(transferencias, "obtener_conexion", prestar)
    monkeypatch.setattr(transferencias, "_procesar_transferencia", intento)
    monkeypatch.setattr(transferencias, "obtener_sqlstate", lambda _: "57014")
    monkeypatch.setattr(transferencias.time, "sleep", lambda _: eventos.append(("dormir", None)))
    resultado = transferencias.procesar_transferencia(id_idempotencia="misma-clave")
    assert resultado["estado"] == "COMPLETADA"
    assert llamadas[0][1] == llamadas[1][1] == "misma-clave"
    assert llamadas[0][0] is not llamadas[1][0]
    assert [evento for evento, _ in eventos] == [
        "adquirir", "devolver", "dormir", "adquirir", "devolver",
    ]


def test_pool_espera_y_timeout_acotado(monkeypatch):
    pool_interno = MagicMock()
    pool_interno.getconn.return_value = MagicMock()
    limitado = conexion.PoolConEspera(pool_interno, capacidad=1)
    monkeypatch.setattr(conexion.settings, "db_pool_timeout_s", 0.01)
    primero = limitado.getconn()
    inicio = __import__("time").perf_counter()
    with pytest.raises(conexion.PoolAgotado):
        limitado.getconn()
    duracion = __import__("time").perf_counter() - inicio
    assert 0.005 <= duracion < 0.2
    limitado.putconn(primero)
    segundo = limitado.getconn()
    limitado.putconn(segundo)
    assert pool_interno.getconn.call_count == 2
    assert pool_interno.putconn.call_count == 2


def test_pool_cola_llena_rechaza_sin_fuga(monkeypatch):
    import threading
    import time

    pool_interno = MagicMock()
    limitado = conexion.PoolConEspera(pool_interno, capacidad=1)
    limitado.cola = threading.BoundedSemaphore(1)
    monkeypatch.setattr(conexion.settings, "db_pool_timeout_s", 0.05)
    primero = limitado.getconn()
    esperando = threading.Event()
    fallo = []

    def esperar():
        esperando.set()
        try:
            limitado.getconn()
        except conexion.PoolAgotado as exc:
            fallo.append(exc)

    hilo = threading.Thread(target=esperar)
    hilo.start()
    assert esperando.wait(1)
    # Confirmar que el hilo entro en la cola antes de probar su limite.
    limite = time.perf_counter() + 1
    while limitado.cola._value != 0 and time.perf_counter() < limite:
        time.sleep(0.001)
    assert limitado.cola._value == 0
    with pytest.raises(conexion.PoolAgotado, match="Cola"):
        limitado.getconn()
    hilo.join(timeout=1)
    assert len(fallo) == 1
    limitado.putconn(primero)
    segundo = limitado.getconn()
    limitado.putconn(segundo)

def test_timeout_de_adquisicion_devuelve_503(monkeypatch):
    from fastapi.testclient import TestClient
    from servicio_transacciones.app.main import app

    def pool_saturado():
        raise conexion.PoolAgotado("Tiempo de espera del pool agotado")
        yield

    monkeypatch.setattr(transferencias, "obtener_conexion", pool_saturado)
    respuesta = TestClient(app).post("/transacciones", json={
        "id_idempotencia": "pool-timeout",
        "cuenta_origen_id": "11111111-1111-1111-1111-111111111111",
        "cuenta_destino_id": "22222222-2222-2222-2222-222222222222",
        "monto": "10.00", "divisa": "USD",
    })
    assert respuesta.status_code == 503
    assert respuesta.headers["retry-after"] == "1"

def test_instrumentacion_commit_sin_lectura_post_commit(flujo, monkeypatch):
    conn, fila, args, debit, credit = flujo
    histograma = MagicMock()
    monkeypatch.setattr(transferencias, "DB_SQL_DURACION_SECONDS", histograma)
    transferencias.procesar_transferencia(*args)
    operaciones = {
        llamada.kwargs["operacion"]
        for llamada in histograma.labels.call_args_list
    }
    assert "commit" in operaciones
    assert "lectura_post_commit" not in operaciones


def test_respuesta_exitosa_sin_sql_despues_del_commit(flujo):
    conn, fila, args, debit, credit = flujo
    cur = conn.cursor.return_value.__enter__.return_value
    eventos = []
    conn.__exit__.side_effect = lambda *a: eventos.append("commit")
    conn.cursor.side_effect = lambda *a, **k: (
        eventos.append("cursor") or conn.cursor.return_value)
    conn.rollback.side_effect = lambda: eventos.append("rollback")
    cur.execute.side_effect = lambda *a, **k: eventos.append("sql")
    resultado = transferencias.procesar_transferencia(*args)
    assert eventos[-1] == "commit"  # nada (cursor, SQL, rollback) tras el COMMIT
    assert resultado == {
        "id_transaccion": "tx", "id_idempotencia": "clave",
        "cuenta_origen_id": "origen", "cuenta_destino_id": "destino",
        "monto": Decimal("10.00"), "divisa": "USD", "estado": "COMPLETADA",
        "razon_fallo": None, "creado_en": "ahora", "reproducida": False,
    }


def test_respuesta_reproducida_usa_fila_previa_sin_sql_extra(flujo, monkeypatch):
    conn, fila, args, debit, credit = flujo
    previa = {
        "id_transaccion": "tx0", "id_idempotencia": "clave",
        "cuenta_origen_id": "origen", "cuenta_destino_id": "destino",
        "monto": Decimal("10.00"), "divisa": "USD", "estado": "RECHAZADA",
        "razon_fallo": "SALDO_INSUFICIENTE", "creado_en": "antes",
        "hash_solicitud": "hash", "completado_en": None,
    }
    monkeypatch.setattr(transferencias, "obtener_o_crear_transaccion", lambda **k: {
        "creada": False, "transaccion": previa,
    })
    resultado = transferencias.procesar_transferencia(*args)
    assert resultado["reproducida"] is True
    assert resultado["creado_en"] == "antes"
    assert resultado["estado"] == "RECHAZADA"
    assert resultado["razon_fallo"] == "SALDO_INSUFICIENTE"
    debit.assert_not_called()
    conn.rollback.assert_not_called()


def test_rechazo_construye_respuesta_sin_lectura(flujo, monkeypatch):
    conn, fila, args, debit, credit = flujo
    debit.return_value = None
    monkeypatch.setattr(transferencias, "obtener_cuentas_para_actualizar", lambda *a: {
        "origen": {"estado": "ACTIVA", "divisa": "USD", "saldo": Decimal("1.00")},
        "destino": {"estado": "ACTIVA", "divisa": "USD", "saldo": Decimal("0.00")},
    })
    resultado = transferencias.procesar_transferencia(*args)
    assert resultado["estado"] == "RECHAZADA"
    assert resultado["razon_fallo"] == "SALDO_INSUFICIENTE"
    assert resultado["creado_en"] == "ahora"
    conn.rollback.assert_not_called()


def test_instrumentacion_pool_separa_fases(monkeypatch):
    interno = MagicMock()
    interno.getconn.return_value = MagicMock()
    semaforo = MagicMock()
    getconn = MagicMock()
    putconn = MagicMock()
    monkeypatch.setattr(conexion, "DB_POOL_SEMAFORO_WAIT_SECONDS", semaforo)
    monkeypatch.setattr(conexion, "DB_POOL_GETCONN_SECONDS", getconn)
    monkeypatch.setattr(conexion, "DB_POOL_PUTCONN_SECONDS", putconn)
    limitado = conexion.PoolConEspera(interno, capacidad=1)
    prestada = limitado.getconn()
    limitado.putconn(prestada)
    semaforo.labels.return_value.observe.assert_called_once()
    getconn.labels.return_value.observe.assert_called_once()
    putconn.labels.return_value.observe.assert_called_once()


def test_instrumentacion_ruta_y_threadpool(monkeypatch):
    from datetime import datetime, timezone
    from uuid import UUID
    from fastapi.testclient import TestClient
    from servicio_transacciones.app.main import app
    from servicio_transacciones.app.rutas import transferencias as rutas
    from servicio_transacciones.app import main

    cola = MagicMock()
    ruta = MagicMock()
    tokens = MagicMock()
    monkeypatch.setattr(rutas, "THREADPOOL_QUEUE_SECONDS", cola)
    monkeypatch.setattr(rutas, "RUTA_TRANSACCION_DURACION_SECONDS", ruta)
    monkeypatch.setattr(main, "THREADPOOL_TOKENS_TOTAL", tokens)
    monkeypatch.setattr(rutas, "procesar_transferencia", lambda **k: {
        "id_transaccion": UUID("33333333-3333-3333-3333-333333333333"),
        "id_idempotencia": k["id_idempotencia"],
        "cuenta_origen_id": k["cuenta_origen_id"],
        "cuenta_destino_id": k["cuenta_destino_id"],
        "monto": k["monto"], "divisa": k["divisa"],
        "estado": "COMPLETADA", "razon_fallo": None,
        "creado_en": datetime.now(timezone.utc), "reproducida": False,
    })
    respuesta = TestClient(app).post("/transacciones", json={
        "id_idempotencia": "instrumentacion",
        "cuenta_origen_id": "11111111-1111-1111-1111-111111111111",
        "cuenta_destino_id": "22222222-2222-2222-2222-222222222222",
        "monto": "10.00", "divisa": "USD",
    })
    assert respuesta.status_code == 201
    cola.labels.return_value.observe.assert_called_once()
    ruta.labels.return_value.observe.assert_called_once()
    assert tokens.labels.return_value.set.call_count >= 1

def test_pool_minimo_15_maximo_15_presupuesto_75_conexiones(monkeypatch):
    interno = MagicMock()
    constructor = MagicMock(return_value=interno)
    monkeypatch.setattr(conexion, "ThreadedConnectionPool", constructor)
    monkeypatch.setattr(conexion, "_pool", None)
    monkeypatch.setattr(conexion, "_pool_pid", None)
    pool = conexion._obtener_pool()
    assert constructor.call_args.kwargs["minconn"] == 15
    assert constructor.call_args.kwargs["maxconn"] == 15
    assert pool.pool is interno
    assert pool.slots._value == 15


def test_timeout_adquisicion_pool_005s():
    assert conexion.settings.db_pool_timeout_s == 0.05
