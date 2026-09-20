import hashlib
import os
import uuid

import psycopg2
import pytest
from dotenv import load_dotenv

from servicio_transacciones.app.repositorios.transacciones import (
    obtener_transaccion_por_idempotencia,
)

from servicio_transacciones.app.servicios.idempotencia import (
    ConflictoIdempotencia,
    validar_idempotencia,
)

from servicio_transacciones.app.servicios.idempotencia import (
    ConflictoIdempotencia,
    obtener_o_crear_transaccion,
    validar_idempotencia,
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


def _connect():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=ADMIN_USER,
        password=ADMIN_PASS,
    )


@pytest.fixture
def conn():
    conexion = _connect()

    yield conexion

    conexion.rollback()
    conexion.close()


def crear_cuenta(
    cur,
    saldo=100,
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
            "Titular de prueba",
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


def insertar_transaccion(
    cur,
    idempotencia,
    hash_solicitud,
    origen,
    destino,
    monto,
):
    cur.execute(
        """
        INSERT INTO transacciones (
            id_idempotencia,
            hash_solicitud,
            cuenta_origen_id,
            cuenta_destino_id,
            monto
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id_transaccion
        """,
        (
            idempotencia,
            hash_solicitud,
            origen,
            destino,
            monto,
        ),
    )

    return cur.fetchone()[0]

def test_idempotencia_nueva_permite_procesar(
    conn,
):
    with conn.cursor() as cur:

        clave = (
            f"idem-{uuid.uuid4().hex}"
        )

        existente = (
            obtener_transaccion_por_idempotencia(
                cur,
                clave,
            )
        )

        resultado = validar_idempotencia(
            existente,
            "a" * 64,
        )

        assert resultado is None

def test_misma_idempotencia_y_mismo_hash(
    conn,
):
    with conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        clave = (
            f"idem-{uuid.uuid4().hex}"
        )

        hash_solicitud = generar_hash(
            origen,
            destino,
            100,
        )

        transaccion_id = insertar_transaccion(
            cur,
            clave,
            hash_solicitud,
            origen,
            destino,
            100,
        )

        existente = (
            obtener_transaccion_por_idempotencia(
                cur,
                clave,
            )
        )

        resultado = validar_idempotencia(
            existente,
            hash_solicitud,
        )

        assert resultado is not None

        assert (
            resultado["id_transaccion"]
            == transaccion_id
        )

        assert (
            resultado["id_idempotencia"]
            == clave
        )

def test_misma_idempotencia_con_hash_diferente(
    conn,
):
    with conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        clave = (
            f"idem-{uuid.uuid4().hex}"
        )

        hash_original = generar_hash(
            origen,
            destino,
            100,
        )

        insertar_transaccion(
            cur,
            clave,
            hash_original,
            origen,
            destino,
            100,
        )

        existente = (
            obtener_transaccion_por_idempotencia(
                cur,
                clave,
            )
        )

        hash_nuevo = generar_hash(
            origen,
            destino,
            500,
        )

        with pytest.raises(
            ConflictoIdempotencia
        ):
            validar_idempotencia(
                existente,
                hash_nuevo,
            )

def test_obtener_transaccion_por_idempotencia(
    conn,
):
    with conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        clave = (
            f"idem-{uuid.uuid4().hex}"
        )

        hash_solicitud = generar_hash(
            origen,
            destino,
            25,
        )

        transaccion_id = insertar_transaccion(
            cur,
            clave,
            hash_solicitud,
            origen,
            destino,
            25,
        )

        resultado = (
            obtener_transaccion_por_idempotencia(
                cur,
                clave,
            )
        )

        assert resultado is not None

        assert (
            resultado["id_transaccion"]
            == transaccion_id
        )

        assert (
            resultado["hash_solicitud"]
            == hash_solicitud
        )

        assert (
            resultado["estado"]
            == "PENDIENTE"
        )

def test_obtener_o_crear_crea_transaccion_nueva(
    conn,
):
    with conn.cursor() as cur:
        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

    clave = f"idem-{uuid.uuid4().hex}"

    hash_solicitud = generar_hash(
        origen,
        destino,
        50,
    )

    resultado = obtener_o_crear_transaccion(
        conn,
        clave,
        hash_solicitud,
        origen,
        destino,
        50,
    )

    assert resultado["creada"] is True

    assert (
        resultado["transaccion"]["estado"]
        == "PENDIENTE"
    )

def test_obtener_o_crear_devuelve_creado_en_de_la_fila(
    conn,
):
    with conn.cursor() as cur:
        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

    clave = f"idem-{uuid.uuid4().hex}"
    hash_solicitud = generar_hash(origen, destino, 50)

    creada = obtener_o_crear_transaccion(
        conn, clave, hash_solicitud, origen, destino, 50,
    )
    with conn.cursor() as cur:
        fila = obtener_transaccion_por_idempotencia(cur, clave)

    assert creada["transaccion"]["creado_en"] == fila["creado_en"]


def test_obtener_o_crear_reutiliza_transaccion(
    conn,
):
    with conn.cursor() as cur:
        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

    clave = f"idem-{uuid.uuid4().hex}"

    hash_solicitud = generar_hash(
        origen,
        destino,
        50,
    )

    primera = obtener_o_crear_transaccion(
        conn,
        clave,
        hash_solicitud,
        origen,
        destino,
        50,
    )

    segunda = obtener_o_crear_transaccion(
        conn,
        clave,
        hash_solicitud,
        origen,
        destino,
        50,
    )

    assert primera["creada"] is True
    assert segunda["creada"] is False

    assert (
        primera["transaccion"]["id_transaccion"]
        == segunda["transaccion"]["id_transaccion"]
    )

def test_obtener_o_crear_detecta_conflicto(
    conn,
):
    with conn.cursor() as cur:
        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

    clave = f"idem-{uuid.uuid4().hex}"

    hash_original = generar_hash(
        origen,
        destino,
        50,
    )

    obtener_o_crear_transaccion(
        conn,
        clave,
        hash_original,
        origen,
        destino,
        50,
    )

    hash_distinto = generar_hash(
        origen,
        destino,
        500,
    )

    with pytest.raises(
        ConflictoIdempotencia
    ):
        obtener_o_crear_transaccion(
            conn,
            clave,
            hash_distinto,
            origen,
            destino,
            500,
        )