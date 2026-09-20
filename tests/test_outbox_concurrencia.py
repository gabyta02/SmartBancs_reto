import os
import threading
import uuid

import psycopg2
from dotenv import load_dotenv

from worker_outbox.app.servicios.outbox import (
    reclamar_lote_outbox,
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
            f"idem-{uuid.uuid4().hex}",
            "b" * 64,
            str(origen),
            str(destino),
            "10.00",
            "USD",
        ),
    )

    return cur.fetchone()[0]


def crear_evento(cur, transaccion_id):
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
            %s,
            %s::jsonb,
            'PENDIENTE',
            0
        )
        RETURNING id_eventos
        """,
        (
            str(transaccion_id),
            "TRANSFERENCIA_COMPLETADA",
            '{"test": true}',
        ),
    )

    return cur.fetchone()[0]


def test_dos_workers_no_reclaman_mismo_evento():
    conn_admin = conectar()

    try:
        with conn_admin.cursor() as cur:
            transaccion_id = crear_transaccion(cur)
            id_eventos = crear_evento(
                cur,
                transaccion_id,
            )

        conn_admin.commit()

        barrera = threading.Barrier(2)

        resultados = []
        errores = []

        def worker():
            conn = conectar()

            try:
                barrera.wait()

                eventos = reclamar_lote_outbox(
                    conn,
                    limite=1,
                )

                resultados.append(eventos)

            except Exception as exc:
                errores.append(exc)

            finally:
                conn.close()

        hilo_1 = threading.Thread(
            target=worker
        )

        hilo_2 = threading.Thread(
            target=worker
        )

        hilo_1.start()
        hilo_2.start()

        hilo_1.join()
        hilo_2.join()

        assert errores == []

        eventos_reclamados = []

        for lote in resultados:
            eventos_reclamados.extend(lote)

        ids = [
            str(evento["id_eventos"])
            for evento in eventos_reclamados
        ]

        assert ids.count(
            str(id_eventos)
        ) == 1

        with conn_admin.cursor() as cur:
            cur.execute(
                """
                SELECT estado
                FROM eventos_outbox
                WHERE id_eventos = %s
                """,
                (str(id_eventos),),
            )

            estado = cur.fetchone()[0]

        assert estado == "PROCESANDO"

    finally:
        conn_admin.rollback()
        conn_admin.close()