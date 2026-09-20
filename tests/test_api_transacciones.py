import os
import uuid
from decimal import Decimal

import psycopg2
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from servicio_transacciones.app.main import app



load_dotenv()

client = TestClient(app)

DB_HOST = os.getenv("POSTGRES_HOST", "127.0.0.1")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB")
ADMIN_USER = os.getenv("POSTGRES_USER")
ADMIN_PASS = os.getenv("POSTGRES_PASSWORD")


def conectar_admin():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=ADMIN_USER,
        password=ADMIN_PASS,
    )


def crear_cuenta(cur, saldo):
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
            "Titular API",
            saldo,
        ),
    )

    return cur.fetchone()[0]

client = TestClient(app)


def test_health():
    response = client.get(
        "/health"
    )

    assert response.status_code == 200

    assert (
        response.json()["status"]
        == "ok"
    )

def test_transferencia_rechaza_monto_negativo():

    response = client.post(
        "/transacciones",
        json={
            "id_idempotencia": "test-monto",
            "cuenta_origen_id":
                "11111111-1111-1111-1111-111111111111",
            "cuenta_destino_id":
                "22222222-2222-2222-2222-222222222222",
            "monto": -10,
            "divisa": "USD",
        },
    )

    assert response.status_code == 422

def test_transferencia_rechaza_misma_cuenta():

    cuenta = (
        "11111111-1111-1111-1111-111111111111"
    )

    response = client.post(
        "/transacciones",
        json={
            "id_idempotencia":
                "test-misma-cuenta",
            "cuenta_origen_id":
                cuenta,
            "cuenta_destino_id":
                cuenta,
            "monto":
                10,
            "divisa":
                "USD",
        },
    )

    assert response.status_code == 422


def test_transferencia_cuenta_inexistente():
    response = client.post(
        "/transacciones",
        json={
            "id_idempotencia": f"idem-inexistente-{uuid.uuid4().hex}",
            "cuenta_origen_id": str(uuid.uuid4()),
            "cuenta_destino_id": str(uuid.uuid4()),
            "monto": 10,
            "divisa": "USD",
        },
    )

    assert response.status_code == 404

def test_transferencia_e2e_completa():
    conn = conectar_admin()

    try:
        with conn.cursor() as cur:
            origen = crear_cuenta(
                cur,
                Decimal("100.00"),
            )

            destino = crear_cuenta(
                cur,
                Decimal("50.00"),
            )

        conn.commit()

        clave = f"idem-api-{uuid.uuid4().hex}"

        response = client.post(
            "/transacciones",
            json={
                "id_idempotencia": clave,
                "cuenta_origen_id": str(origen),
                "cuenta_destino_id": str(destino),
                "monto": 30,
                "divisa": "USD",
            },
        )

        assert response.status_code == 201

        data = response.json()

        assert data["estado"] == "COMPLETADA"
        assert data["reproducida"] is False

        transaccion_id = data["id_transaccion"]

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
                SELECT estado
                FROM transacciones
                WHERE id_transaccion = %s
                """,
                (transaccion_id,),
            )

            estado = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM movimientos
                WHERE transaccion_id = %s
                """,
                (transaccion_id,),
            )

            movimientos = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM eventos_outbox
                WHERE transaccion_id = %s
                """,
                (transaccion_id,),
            )

            eventos = cur.fetchone()[0]

            cur.execute(
                "SELECT carga_util FROM eventos_outbox WHERE transaccion_id = %s",
                (transaccion_id,),
            )
            carga_outbox = cur.fetchone()[0]

        assert saldo_origen == Decimal("70.00")
        assert saldo_destino == Decimal("80.00")

        assert estado == "COMPLETADA"
        assert movimientos == 2
        assert eventos == 1
        assert carga_outbox["trace_context"]["traceparent"]

    finally:
        conn.rollback()
        conn.close()

def test_transferencia_http_es_idempotente():
    conn = conectar_admin()

    try:
        with conn.cursor() as cur:
            origen = crear_cuenta(
                cur,
                Decimal("100.00"),
            )

            destino = crear_cuenta(
                cur,
                Decimal("0.00"),
            )

        conn.commit()

        clave = f"idem-api-{uuid.uuid4().hex}"

        payload = {
            "id_idempotencia": clave,
            "cuenta_origen_id": str(origen),
            "cuenta_destino_id": str(destino),
            "monto": 25,
            "divisa": "USD",
        }

        primera = client.post(
            "/transacciones",
            json=payload,
        )

        segunda = client.post(
            "/transacciones",
            json=payload,
        )

        assert primera.status_code == 201
        assert segunda.status_code == 201

        data_1 = primera.json()
        data_2 = segunda.json()

        assert (
            data_1["id_transaccion"]
            == data_2["id_transaccion"]
        )

        assert data_1["reproducida"] is False
        assert data_2["reproducida"] is True

        conflicto = client.post(
            "/transacciones",
            json={**payload, "monto": 26},
        )
        assert conflicto.status_code == 409

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

        assert saldo_origen == Decimal("75.00")
        assert saldo_destino == Decimal("25.00")

    finally:
        conn.rollback()
        conn.close()
