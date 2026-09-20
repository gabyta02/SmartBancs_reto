import os
import uuid

import psycopg2
from dotenv import load_dotenv

from worker_outbox.app.servicios.outbox import (
    reclamar_lote_outbox,
    registrar_evento_publicado,
    registrar_fallo_procesamiento,
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
            "Titular Outbox",
            saldo,
        ),
    )

    return cur.fetchone()[0]


def crear_transaccion(cur):
    cuenta_origen = crear_cuenta(cur, "100.00")
    cuenta_destino = crear_cuenta(cur, "0.00")

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
            f"idem-{uuid.uuid4().hex}",
            "a" * 64,
            str(cuenta_origen),
            str(cuenta_destino),
            "10.00",
            "USD",
        ),
    )

    return cur.fetchone()[0]


def crear_evento_outbox(
    cur,
    transaccion_id,
    estado="PENDIENTE",
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
        VALUES (%s, %s, %s::jsonb, %s, %s)
        RETURNING id_eventos
        """,
        (
            str(transaccion_id),
            "TRANSFERENCIA_COMPLETADA",
            '{"test": true}',
            estado,
            intentos,
        ),
    )

    return cur.fetchone()[0]


def obtener_evento(cur, id_eventos):
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


def test_reclamar_evento_pendiente_lo_marca_procesando():
    conn = conectar()

    try:
        with conn.cursor() as cur:
            transaccion_id = crear_transaccion(cur)

            id_eventos = crear_evento_outbox(
                cur,
                transaccion_id,
                estado="PENDIENTE",
            )

        conn.commit()

        eventos = reclamar_lote_outbox(
            conn,
            limite=10,
        )

        ids = {
            str(evento["id_eventos"])
            for evento in eventos
        }

        assert str(id_eventos) in ids

        with conn.cursor() as cur:
            estado, intentos, publicado_en = obtener_evento(
                cur,
                id_eventos,
            )

        assert estado == "PROCESANDO"
        assert intentos == 0
        assert publicado_en is None

    finally:
        conn.rollback()
        conn.close()


def test_no_reclama_evento_que_no_esta_pendiente():
    conn = conectar()

    try:
        with conn.cursor() as cur:
            transaccion_id = crear_transaccion(cur)

            id_eventos = crear_evento_outbox(
                cur,
                transaccion_id,
                estado="PUBLICADO",
            )

        conn.commit()

        eventos = reclamar_lote_outbox(
            conn,
            limite=10,
        )

        ids = {
            str(evento["id_eventos"])
            for evento in eventos
        }

        assert str(id_eventos) not in ids

    finally:
        conn.rollback()
        conn.close()


def test_marcar_evento_publicado():
    conn = conectar()

    try:
        with conn.cursor() as cur:
            transaccion_id = crear_transaccion(cur)

            id_eventos = crear_evento_outbox(
                cur,
                transaccion_id,
                estado="PENDIENTE",
            )

        conn.commit()

        eventos = reclamar_lote_outbox(
            conn,
            limite=10,
        )

        evento = next(
            e
            for e in eventos
            if str(e["id_eventos"]) == str(id_eventos)
        )

        registrar_evento_publicado(
            conn,
            evento["id_eventos"],
        )

        with conn.cursor() as cur:
            estado, intentos, publicado_en = obtener_evento(
                cur,
                id_eventos,
            )

        assert estado == "PUBLICADO"
        assert intentos == 0
        assert publicado_en is not None

    finally:
        conn.rollback()
        conn.close()


def test_fallo_devuelve_evento_a_pendiente_e_incrementa_intentos():
    conn = conectar()

    try:
        with conn.cursor() as cur:
            transaccion_id = crear_transaccion(cur)

            id_eventos = crear_evento_outbox(
                cur,
                transaccion_id,
                estado="PENDIENTE",
                intentos=0,
            )

        conn.commit()

        eventos = reclamar_lote_outbox(
            conn,
            limite=10,
        )

        evento = next(
            e
            for e in eventos
            if str(e["id_eventos"]) == str(id_eventos)
        )

        resultado = registrar_fallo_procesamiento(
            conn,
            evento,
        )

        assert resultado == "PENDIENTE"

        with conn.cursor() as cur:
            estado, intentos, publicado_en = obtener_evento(
                cur,
                id_eventos,
            )

        assert estado == "PENDIENTE"
        assert intentos == 1
        assert publicado_en is None

    finally:
        conn.rollback()
        conn.close()


def test_tercer_fallo_marca_evento_como_fallido():
    conn = conectar()

    try:
        with conn.cursor() as cur:
            transaccion_id = crear_transaccion(cur)

            id_eventos = crear_evento_outbox(
                cur,
                transaccion_id,
                estado="PENDIENTE",
                intentos=2,
            )

        conn.commit()

        eventos = reclamar_lote_outbox(
            conn,
            limite=10,
        )

        evento = next(
            e
            for e in eventos
            if str(e["id_eventos"]) == str(id_eventos)
        )

        resultado = registrar_fallo_procesamiento(
            conn,
            evento,
        )

        assert resultado == "FALLIDO"

        with conn.cursor() as cur:
            estado, intentos, publicado_en = obtener_evento(
                cur,
                id_eventos,
            )

        assert estado == "FALLIDO"
        assert intentos == 3
        assert publicado_en is None

    finally:
        conn.rollback()
        conn.close()