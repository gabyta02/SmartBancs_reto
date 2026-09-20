import hashlib
import os
import uuid
from decimal import Decimal

import psycopg2
import pytest

from dotenv import load_dotenv

from servicio_transacciones.app.servicios.transferencias import (
    procesar_transferencia,
)


load_dotenv()


DB_HOST = os.getenv(
    "POSTGRES_HOST",
    "127.0.0.1",
)

DB_PORT = os.getenv(
    "POSTGRES_PORT",
    "5432",
)

DB_NAME = os.getenv(
    "POSTGRES_DB",
)

ADMIN_USER = os.getenv(
    "POSTGRES_USER",
)

ADMIN_PASS = os.getenv(
    "POSTGRES_PASSWORD",
)


def conectar():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=ADMIN_USER,
        password=ADMIN_PASS,
    )


@pytest.fixture
def conn():
    conexion = conectar()

    yield conexion

    conexion.rollback()
    conexion.close()


def crear_cuenta(
    cur,
    saldo,
):
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

def test_transferencia_actualiza_saldos(
    conn,
):
    with conn.cursor() as cur:
        origen = crear_cuenta(
            cur,
            saldo=100,
        )

        destino = crear_cuenta(
            cur,
            saldo=50,
        )

    clave = f"idem-{uuid.uuid4().hex}"

    monto = Decimal("30.00")

    hash_solicitud = generar_hash(
        origen,
        destino,
        monto,
    )

    resultado = procesar_transferencia(
        conn=conn,
        id_idempotencia=clave,
        hash_solicitud=hash_solicitud,
        cuenta_origen_id=origen,
        cuenta_destino_id=destino,
        monto=monto,
    )

    assert (
        resultado["estado"]
        == "COMPLETADA"
    )

    with conn.cursor() as cur:

        cur.execute(
            """
            SELECT saldo
            FROM cuentas
            WHERE id_cuenta = %s
            """,
            (origen,),
        )

        saldo_origen = cur.fetchone()[0]

        cur.execute(
            """
            SELECT saldo
            FROM cuentas
            WHERE id_cuenta = %s
            """,
            (destino,),
        )

        saldo_destino = cur.fetchone()[0]

    assert (
        saldo_origen
        == Decimal("70.00")
    )

    assert (
        saldo_destino
        == Decimal("80.00")
    )

def test_transferencia_crea_dos_movimientos(
    conn,
):
    with conn.cursor() as cur:

        origen = crear_cuenta(
            cur,
            100,
        )

        destino = crear_cuenta(
            cur,
            50,
        )

    monto = Decimal("20.00")

    clave = f"idem-{uuid.uuid4().hex}"

    resultado = procesar_transferencia(
        conn,
        clave,
        generar_hash(
            origen,
            destino,
            monto,
        ),
        origen,
        destino,
        monto,
    )

    transaccion_id = (
        resultado["id_transaccion"]
    )

    with conn.cursor() as cur:

        cur.execute(
            """
            SELECT tipo, monto
            FROM movimientos
            WHERE transaccion_id = %s
            ORDER BY tipo
            """,
            (transaccion_id,),
        )

        movimientos = cur.fetchall()

    assert len(movimientos) == 2

    tipos = {
        movimiento[0]
        for movimiento in movimientos
    }

    assert tipos == {
        "DEBITO",
        "CREDITO",
    }

def test_saldo_insuficiente_no_mueve_dinero(
    conn,
):
    with conn.cursor() as cur:

        origen = crear_cuenta(
            cur,
            20,
        )

        destino = crear_cuenta(
            cur,
            50,
        )

    monto = Decimal("100.00")

    resultado = procesar_transferencia(
        conn,
        f"idem-{uuid.uuid4().hex}",
        generar_hash(
            origen,
            destino,
            monto,
        ),
        origen,
        destino,
        monto,
    )

    assert (
        resultado["estado"]
        == "RECHAZADA"
    )

    assert (
        resultado["razon_fallo"]
        == "SALDO_INSUFICIENTE"
    )

    with conn.cursor() as cur:

        cur.execute(
            """
            SELECT saldo
            FROM cuentas
            WHERE id_cuenta = %s
            """,
            (origen,),
        )

        assert (
            cur.fetchone()[0]
            == Decimal("20.00")
        )

        cur.execute(
            """
            SELECT saldo
            FROM cuentas
            WHERE id_cuenta = %s
            """,
            (destino,),
        )

        assert (
            cur.fetchone()[0]
            == Decimal("50.00")
        )

def test_transferencia_crea_evento_outbox(
    conn,
):
    with conn.cursor() as cur:

        origen = crear_cuenta(
            cur,
            100,
        )

        destino = crear_cuenta(
            cur,
            100,
        )

    monto = Decimal("10.00")

    resultado = procesar_transferencia(
        conn,
        f"idem-{uuid.uuid4().hex}",
        generar_hash(
            origen,
            destino,
            monto,
        ),
        origen,
        destino,
        monto,
    )

    with conn.cursor() as cur:

        cur.execute(
            """
            SELECT
                tipo_evento,
                estado
            FROM eventos_outbox
            WHERE transaccion_id = %s
            """,
            (
                resultado[
                    "id_transaccion"
                ],
            ),
        )

        evento = cur.fetchone()

    assert evento is not None

    assert (
        evento[0]
        == "TRANSFERENCIA_COMPLETADA"
    )

    assert evento[1] == "PENDIENTE"

def test_error_intermedio_hace_rollback_total(
    conn,
    monkeypatch,
):
    from servicio_transacciones.app import servicios
    from servicio_transacciones.app.servicios import transferencias

    with conn.cursor() as cur:
        origen = crear_cuenta(
            cur,
            saldo=Decimal("100.00"),
        )

        destino = crear_cuenta(
            cur,
            saldo=Decimal("50.00"),
        )

    conn.commit()

    monto = Decimal("30.00")

    clave = f"idem-{uuid.uuid4().hex}"

    hash_solicitud = generar_hash(
        origen,
        destino,
        monto,
    )

    def insertar_movimiento_con_error(*args, **kwargs):
        raise RuntimeError(
            "Error simulado durante el registro del movimiento"
        )

    monkeypatch.setattr(
        transferencias,
        "insertar_movimiento",
        insertar_movimiento_con_error,
    )

    with pytest.raises(RuntimeError):
        procesar_transferencia(
            conn=conn,
            id_idempotencia=clave,
            hash_solicitud=hash_solicitud,
            cuenta_origen_id=origen,
            cuenta_destino_id=destino,
            monto=monto,
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT saldo
            FROM cuentas
            WHERE id_cuenta = %s
            """,
            (origen,),
        )

        saldo_origen = cur.fetchone()[0]

        cur.execute(
            """
            SELECT saldo
            FROM cuentas
            WHERE id_cuenta = %s
            """,
            (destino,),
        )

        saldo_destino = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*)
            FROM transacciones
            WHERE id_idempotencia = %s
            """,
            (clave,),
        )

        transacciones = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*)
            FROM movimientos
            WHERE cuenta_id IN (%s, %s)
            """,
            (
                origen,
                destino,
            ),
        )

        movimientos = cur.fetchone()[0]

    assert saldo_origen == Decimal("100.00")
    assert saldo_destino == Decimal("50.00")

    assert transacciones == 0
    assert movimientos == 0