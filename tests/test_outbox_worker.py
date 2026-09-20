import os
import uuid

import psycopg2
from dotenv import load_dotenv

from worker_outbox.app.main import (
    procesar_lote_outbox,
)


load_dotenv()

DB_HOST = os.getenv("POSTGRES_HOST", "127.0.0.1")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")


def conectar():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def crear_cuenta(cur, saldo="100.00"):
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
            "Titular Worker",
            saldo,
        ),
    )

    return cur.fetchone()[0]


def crear_transaccion(cur):
    origen = crear_cuenta(cur, "100.00")
    destino = crear_cuenta(cur, "0.00")

    cur.execute(
        """
        INSERT INTO transacciones (
            id_idempotencia,
            hash_solicitud,
            cuenta_origen_id,
            cuenta_destino_id,
            monto,
            divisa,
            estado
        )
        VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            'COMPLETADA'
        )
        RETURNING id_transaccion
        """,
        (
            f"idem-worker-{uuid.uuid4().hex}",
            "c" * 64,
            str(origen),
            str(destino),
            "10.00",
            "USD",
        ),
    )

    return cur.fetchone()[0]


def crear_evento(
    cur,
    transaccion_id,
    intentos=0,
):
    cur.execute(
        """
        INSERT INTO eventos_outbox (
            transaccion_id,
            tipo_evento,
            carga_util,
            estado,
            intentos
        )
        VALUES (
            %s,
            'TRANSFERENCIA_COMPLETADA',
            %s::jsonb,
            'PENDIENTE',
            %s
        )
        RETURNING id_eventos
        """,
        (
            str(transaccion_id),
            '{"origen": "test"}',
            intentos,
        ),
    )

    return cur.fetchone()[0]


def obtener_estado(cur, id_eventos):
    cur.execute(
        """
        SELECT
            estado,
            intentos,
            publicado_en
        FROM eventos_outbox
        WHERE id_eventos = %s
        """,
        (str(id_eventos),),
    )

    return cur.fetchone()


def test_worker_publica_evento_exitoso():
    conn = conectar()

    try:
        with conn.cursor() as cur:
            transaccion_id = crear_transaccion(cur)
            id_eventos = crear_evento(
                cur,
                transaccion_id,
            )

        conn.commit()

        procesados = []

        def procesador_falso(evento):
            procesados.append(
                evento["id_eventos"]
            )

        resultado = procesar_lote_outbox(
            conn=conn,
            procesador_evento=procesador_falso,
            limite=20,
        )

        assert str(id_eventos) in {
            str(x)
            for x in procesados
        }

        assert resultado["publicados"] >= 1

        with conn.cursor() as cur:
            estado, intentos, publicado_en = (
                obtener_estado(
                    cur,
                    id_eventos,
                )
            )

        assert estado == "PUBLICADO"
        assert intentos == 0
        assert publicado_en is not None

    finally:
        conn.rollback()
        conn.close()


def test_worker_reintenta_si_procesamiento_falla():
    conn = conectar()

    try:
        with conn.cursor() as cur:
            transaccion_id = crear_transaccion(cur)
            id_eventos = crear_evento(
                cur,
                transaccion_id,
                intentos=0,
            )

        conn.commit()

        def procesador_fallido(evento):
            raise RuntimeError(
                "Servicio externo no disponible"
            )

        resultado = procesar_lote_outbox(
            conn=conn,
            procesador_evento=procesador_fallido,
            limite=20,
        )

        assert resultado["reintentados"] >= 1

        with conn.cursor() as cur:
            estado, intentos, publicado_en = (
                obtener_estado(
                    cur,
                    id_eventos,
                )
            )

        assert estado == "PENDIENTE"
        assert intentos == 1
        assert publicado_en is None

    finally:
        conn.rollback()
        conn.close()


def test_worker_marca_fallido_al_agotar_intentos():
    conn = conectar()

    try:
        with conn.cursor() as cur:
            transaccion_id = crear_transaccion(cur)
            id_eventos = crear_evento(
                cur,
                transaccion_id,
                intentos=2,
            )

        conn.commit()

        def procesador_fallido(evento):
            raise RuntimeError(
                "Servicio externo no disponible"
            )

        resultado = procesar_lote_outbox(
            conn=conn,
            procesador_evento=procesador_fallido,
            limite=20,
        )

        assert resultado["fallidos"] >= 1

        with conn.cursor() as cur:
            estado, intentos, publicado_en = (
                obtener_estado(
                    cur,
                    id_eventos,
                )
            )

        assert estado == "FALLIDO"
        assert intentos == 3
        assert publicado_en is None

    finally:
        conn.rollback()
        conn.close()