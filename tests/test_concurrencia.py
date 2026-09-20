import hashlib
import os
import threading
import uuid
from decimal import Decimal

import psycopg2
import pytest
from dotenv import load_dotenv

from servicio_transacciones.app.servicios.transferencias import (
    procesar_transferencia,
)


load_dotenv()


DB_HOST = os.getenv("POSTGRES_HOST", "127.0.0.1")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB")
ADMIN_USER = os.getenv("POSTGRES_USER")
ADMIN_PASS = os.getenv("POSTGRES_PASSWORD")


def conectar():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=ADMIN_USER,
        password=ADMIN_PASS,
    )


def crear_cuenta(conn, saldo):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cuentas (
                numero_cuenta,
                nombre_titular,
                saldo
            )
            VALUES (%s, %s, %s)
            RETURNING id_cuenta
            """,
            (
                f"TEST-{uuid.uuid4().hex[:20]}",
                "Titular prueba",
                saldo,
            ),
        )

        return cur.fetchone()[0]


def generar_hash(
    origen,
    destino,
    monto,
    divisa="USD",
):
    contenido = (
        f"{origen}|"
        f"{destino}|"
        f"{monto}|"
        f"{divisa}"
    )

    return hashlib.sha256(
        contenido.encode("utf-8")
    ).hexdigest()

def test_no_permite_doble_gasto_concurrente():
    conn_setup = conectar()

    try:
        origen = crear_cuenta(
            conn_setup,
            Decimal("100.00"),
        )

        destino_1 = crear_cuenta(
            conn_setup,
            Decimal("0.00"),
        )

        destino_2 = crear_cuenta(
            conn_setup,
            Decimal("0.00"),
        )

        conn_setup.commit()

        resultados = []
        errores = []

        barrera = threading.Barrier(2)

        def ejecutar(destino):
            conn = conectar()

            try:
                monto = Decimal("80.00")

                clave = f"idem-{uuid.uuid4().hex}"

                hash_solicitud = generar_hash(
                    origen,
                    destino,
                    monto,
                )

                barrera.wait()

                resultado = procesar_transferencia(
                    conn=conn,
                    id_idempotencia=clave,
                    hash_solicitud=hash_solicitud,
                    cuenta_origen_id=origen,
                    cuenta_destino_id=destino,
                    monto=monto,
                )

                resultados.append(resultado)

            except Exception as exc:
                errores.append(exc)

            finally:
                conn.close()

        hilo_1 = threading.Thread(
            target=ejecutar,
            args=(destino_1,),
        )

        hilo_2 = threading.Thread(
            target=ejecutar,
            args=(destino_2,),
        )

        hilo_1.start()
        hilo_2.start()

        hilo_1.join()
        hilo_2.join()

        assert errores == []

        estados = [
            resultado["estado"]
            for resultado in resultados
        ]

        assert estados.count("COMPLETADA") == 1
        assert estados.count("RECHAZADA") == 1

        conn_verificacion = conectar()

        try:
            with conn_verificacion.cursor() as cur:
                cur.execute(
                    """
                    SELECT saldo
                    FROM cuentas
                    WHERE id_cuenta = %s
                    """,
                    (origen,),
                )

                saldo_origen = cur.fetchone()[0]

            assert saldo_origen == Decimal("20.00")

        finally:
            conn_verificacion.close()

    finally:
        conn_setup.rollback()
        conn_setup.close()

def test_transferencias_cruzadas_no_generan_deadlock():
    conn_setup = conectar()

    try:
        cuenta_a = crear_cuenta(
            conn_setup,
            Decimal("100.00"),
        )

        cuenta_b = crear_cuenta(
            conn_setup,
            Decimal("100.00"),
        )

        conn_setup.commit()

        resultados = []
        errores = []

        barrera = threading.Barrier(2)

        def ejecutar(
            origen,
            destino,
        ):
            conn = conectar()

            try:
                monto = Decimal("10.00")

                clave = f"idem-{uuid.uuid4().hex}"

                hash_solicitud = generar_hash(
                    origen,
                    destino,
                    monto,
                )

                barrera.wait()

                resultado = procesar_transferencia(
                    conn=conn,
                    id_idempotencia=clave,
                    hash_solicitud=hash_solicitud,
                    cuenta_origen_id=origen,
                    cuenta_destino_id=destino,
                    monto=monto,
                )

                resultados.append(resultado)

            except Exception as exc:
                errores.append(exc)

            finally:
                conn.close()

        hilo_1 = threading.Thread(
            target=ejecutar,
            args=(
                cuenta_a,
                cuenta_b,
            ),
        )

        hilo_2 = threading.Thread(
            target=ejecutar,
            args=(
                cuenta_b,
                cuenta_a,
            ),
        )

        hilo_1.start()
        hilo_2.start()

        hilo_1.join()
        hilo_2.join()

        assert errores == []

        assert len(resultados) == 2

        assert all(
            resultado["estado"]
            == "COMPLETADA"
            for resultado in resultados
        )

    finally:
        conn_setup.rollback()
        conn_setup.close()
def test_muchos_debitos_mismo_origen_solo_parte_completa():
    preparacion = conectar()
    origen = crear_cuenta(preparacion, Decimal("100.00"))
    destinos = [crear_cuenta(preparacion, Decimal("0.00")) for _ in range(8)]
    preparacion.commit()
    preparacion.close()

    barrera = threading.Barrier(len(destinos))
    resultados = []
    errores = []
    mutex = threading.Lock()

    def enviar(destino):
        conn = conectar()
        try:
            monto = Decimal("30.00")
            barrera.wait()
            resultado = procesar_transferencia(
                conn=conn,
                id_idempotencia=f"fase2-{uuid.uuid4().hex}",
                hash_solicitud=generar_hash(origen, destino, monto),
                cuenta_origen_id=origen,
                cuenta_destino_id=destino,
                monto=monto,
            )
            with mutex:
                resultados.append(resultado)
        except Exception as exc:
            with mutex:
                errores.append(exc)
        finally:
            conn.close()

    hilos = [threading.Thread(target=enviar, args=(destino,)) for destino in destinos]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join(timeout=15)
        assert not hilo.is_alive()
    assert errores == []
    assert sum(r["estado"] == "COMPLETADA" for r in resultados) == 3
    assert sum(r["estado"] == "RECHAZADA" for r in resultados) == 5

    verificacion = conectar()
    with verificacion.cursor() as cur:
        cur.execute("SELECT saldo FROM cuentas WHERE id_cuenta = %s", (origen,))
        assert cur.fetchone()[0] == Decimal("10.00")
        cur.execute(
            """SELECT count(*) FROM movimientos m
               JOIN transacciones t ON t.id_transaccion = m.transaccion_id
               WHERE t.cuenta_origen_id = %s AND t.id_idempotencia LIKE 'fase2-%%'""",
            (origen,),
        )
        assert cur.fetchone()[0] == 6
        cur.execute(
            """SELECT count(*) FROM eventos_outbox e
               JOIN transacciones t ON t.id_transaccion = e.transaccion_id
               WHERE t.cuenta_origen_id = %s AND t.id_idempotencia LIKE 'fase2-%%'""",
            (origen,),
        )
        assert cur.fetchone()[0] == 3
    verificacion.close()


@pytest.mark.parametrize("campo,valor,razon", [
    ("estado", "BLOQUEADA", "CUENTA_DESTINO_NO_ACTIVA"),
    ("divisa", "EUR", "DIVISA_NO_COMPATIBLE"),
])
def test_cambio_concurrente_destino_se_rechaza(campo, valor, razon):
    setup = conectar()
    origen = crear_cuenta(setup, Decimal("100.00"))
    destino = crear_cuenta(setup, Decimal("0.00"))
    setup.commit()
    setup.close()

    actualizador = conectar()
    with actualizador.cursor() as cur:
        cur.execute(
            f"UPDATE cuentas SET {campo} = %s WHERE id_cuenta = %s",
            (valor, destino),
        )

    resultado = []
    errores = []
    inicio = threading.Event()

    def enviar():
        conn = conectar()
        try:
            inicio.set()
            monto = Decimal("10.00")
            resultado.append(procesar_transferencia(
                conn=conn,
                id_idempotencia=f"fase2-{uuid.uuid4().hex}",
                hash_solicitud=generar_hash(origen, destino, monto),
                cuenta_origen_id=origen,
                cuenta_destino_id=destino,
                monto=monto,
            ))
        except Exception as exc:
            errores.append(exc)
        finally:
            conn.close()

    hilo = threading.Thread(target=enviar)
    hilo.start()
    assert inicio.wait(5)
    actualizador.commit()
    actualizador.close()
    hilo.join(timeout=10)
    assert not hilo.is_alive()
    assert errores == []
    assert resultado[0]["estado"] == "RECHAZADA"
    assert resultado[0]["razon_fallo"] == razon

    verificacion = conectar()
    with verificacion.cursor() as cur:
        cur.execute("SELECT saldo FROM cuentas WHERE id_cuenta = %s", (origen,))
        assert cur.fetchone()[0] == Decimal("100.00")
        cur.execute("SELECT saldo FROM cuentas WHERE id_cuenta = %s", (destino,))
        assert cur.fetchone()[0] == Decimal("0.00")
    verificacion.close()

def test_destino_uuid_menor_debito_fallido_revierte_credito():
    setup = conectar()
    cuentas = [
        crear_cuenta(setup, Decimal("0.00")),
        crear_cuenta(setup, Decimal("0.00")),
    ]
    setup.commit()
    setup.close()
    destino, origen = sorted(cuentas)
    clave = f"fase2-{uuid.uuid4().hex}"
    monto = Decimal("10.00")
    conn = conectar()
    try:
        resultado = procesar_transferencia(
            conn=conn, id_idempotencia=clave,
            hash_solicitud=generar_hash(origen, destino, monto),
            cuenta_origen_id=origen, cuenta_destino_id=destino, monto=monto,
        )
        assert resultado["estado"] == "RECHAZADA"
        assert resultado["razon_fallo"] == "SALDO_INSUFICIENTE"
    finally:
        conn.close()

    verificacion = conectar()
    with verificacion.cursor() as cur:
        cur.execute(
            "SELECT id_cuenta, saldo FROM cuentas WHERE id_cuenta IN (%s, %s)",
            (origen, destino),
        )
        assert all(saldo == Decimal("0.00") for _, saldo in cur.fetchall())
        cur.execute(
            """SELECT count(*) FROM movimientos m
               JOIN transacciones t ON t.id_transaccion = m.transaccion_id
               WHERE t.id_idempotencia = %s""",
            (clave,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """SELECT count(*) FROM eventos_outbox e
               JOIN transacciones t ON t.id_transaccion = e.transaccion_id
               WHERE t.id_idempotencia = %s""",
            (clave,),
        )
        assert cur.fetchone()[0] == 0
    verificacion.close()

def test_retry_tras_primer_update_no_duplica_transaccion(monkeypatch):
    from servicio_transacciones.app.servicios import transferencias as servicio

    setup = conectar()
    cuentas = [
        crear_cuenta(setup, Decimal("0.00")),
        crear_cuenta(setup, Decimal("0.00")),
    ]
    origen, destino = sorted(cuentas)
    with setup.cursor() as cur:
        cur.execute("UPDATE cuentas SET saldo = 100.00 WHERE id_cuenta = %s", (origen,))
    setup.commit()
    setup.close()
    clave = f"fase2-{uuid.uuid4().hex}"
    monto = Decimal("10.00")
    original = servicio.acreditar_cuenta
    llamadas = [0]

    def credito_con_fallo(cur, cuenta_id, valor, divisa):
        llamadas[0] += 1
        if llamadas[0] == 1:
            raise psycopg2.errors.QueryCanceled("fallo transitorio simulado")
        return original(cur, cuenta_id, valor, divisa)

    monkeypatch.setattr(servicio, "acreditar_cuenta", credito_con_fallo)
    original_sqlstate = servicio.obtener_sqlstate
    monkeypatch.setattr(
        servicio, "obtener_sqlstate",
        lambda exc: "57014" if isinstance(exc, psycopg2.errors.QueryCanceled)
        else original_sqlstate(exc),
    )
    monkeypatch.setattr(servicio.time, "sleep", lambda _: None)

    conn = conectar()
    try:
        parametros = dict(
            conn=conn, id_idempotencia=clave,
            hash_solicitud=generar_hash(origen, destino, monto),
            cuenta_origen_id=origen, cuenta_destino_id=destino, monto=monto,
        )
        primero = procesar_transferencia(**parametros)
        segundo = procesar_transferencia(**parametros)
        assert primero["estado"] == "COMPLETADA"
        assert segundo["reproducida"] is True
    finally:
        conn.close()

    verificacion = conectar()
    with verificacion.cursor() as cur:
        cur.execute("SELECT saldo FROM cuentas WHERE id_cuenta = %s", (origen,))
        assert cur.fetchone()[0] == Decimal("90.00")
        cur.execute("SELECT saldo FROM cuentas WHERE id_cuenta = %s", (destino,))
        assert cur.fetchone()[0] == Decimal("10.00")
        cur.execute("SELECT count(*) FROM transacciones WHERE id_idempotencia = %s", (clave,))
        assert cur.fetchone()[0] == 1
        cur.execute(
            """SELECT count(*) FROM movimientos m
               JOIN transacciones t ON t.id_transaccion = m.transaccion_id
               WHERE t.id_idempotencia = %s""",
            (clave,),
        )
        assert cur.fetchone()[0] == 2
        cur.execute(
            """SELECT count(*) FROM eventos_outbox e
               JOIN transacciones t ON t.id_transaccion = e.transaccion_id
               WHERE t.id_idempotencia = %s""",
            (clave,),
        )
        assert cur.fetchone()[0] == 1
    verificacion.close()
