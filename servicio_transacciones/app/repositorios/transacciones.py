from typing import Optional
from functools import wraps
import time

from psycopg2.extensions import cursor
from psycopg2 import errors

from servicio_transacciones.app.observabilidad.metricas import (
    DB_SQL_DURACION_SECONDS,
    worker_actual,
)


def medir_sql(operacion):
    """Mide el viaje SQL y el fetch de una operacion de repositorio."""
    def decorar(funcion):
        @wraps(funcion)
        def ejecutar(*args, **kwargs):
            inicio = time.perf_counter()
            try:
                return funcion(*args, **kwargs)
            finally:
                DB_SQL_DURACION_SECONDS.labels(
                    worker=worker_actual(), operacion=operacion,
                ).observe(time.perf_counter() - inicio)
        return ejecutar
    return decorar

@medir_sql("lectura_idempotencia")
def obtener_transaccion_por_idempotencia(
    cur: cursor,
    id_idempotencia: str,
) -> Optional[dict]:
    """
    Busca una transacción por su clave de idempotencia.

    Retorna:
        dict con los datos de la transacción
        o None si no existe.
    """

    cur.execute(
        """
        SELECT
            id_transaccion,
            id_idempotencia,
            hash_solicitud,
            cuenta_origen_id,
            cuenta_destino_id,
            monto,
            divisa,
            estado,
            razon_fallo,
            creado_en,
            completado_en
        FROM transacciones
        WHERE id_idempotencia = %s
        """,
        (id_idempotencia,),
    )

    fila = cur.fetchone()

    if fila is None:
        return None

    return {
        "id_transaccion": fila[0],
        "id_idempotencia": fila[1],
        "hash_solicitud": fila[2].strip(),
        "cuenta_origen_id": fila[3],
        "cuenta_destino_id": fila[4],
        "monto": fila[5],
        "divisa": fila[6],
        "estado": fila[7],
        "razon_fallo": fila[8],
        "creado_en": fila[9],
        "completado_en": fila[10],
    }

@medir_sql("insert_transaccion")
def crear_transaccion_idempotente(
    cur,
    id_idempotencia,
    hash_solicitud,
    cuenta_origen_id,
    cuenta_destino_id,
    monto,
    divisa="USD",
):
    """
    Intenta crear una transacción.

    Retorna:
        (id_transaccion, creado_en) de la nueva transacción.

    Puede lanzar:
        UniqueViolation si otra petición concurrente
        insertó la misma clave de idempotencia.
    """

    cur.execute(
        """
        INSERT INTO transacciones (
            id_idempotencia,
            hash_solicitud,
            cuenta_origen_id,
            cuenta_destino_id,
            monto,
            divisa
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id_transaccion, creado_en
        """,
        (
            id_idempotencia,
            hash_solicitud,
            cuenta_origen_id,
            cuenta_destino_id,
            monto,
            divisa,
        ),
    )

    return cur.fetchone()

@medir_sql("bloquear_cuentas_fallback")
def obtener_cuentas_para_actualizar(
    cur,
    cuenta_origen_id,
    cuenta_destino_id,
):
    """
    Obtiene y bloquea las dos cuentas involucradas.

    El ORDER BY garantiza que todas las transferencias
    soliciten los locks en el mismo orden, reduciendo
    el riesgo de deadlocks.
    """

    cur.execute(
        """
        SELECT
            id_cuenta,
            saldo,
            divisa,
            estado
        FROM cuentas
        WHERE id_cuenta IN (%s, %s)
        ORDER BY id_cuenta
        FOR NO KEY UPDATE
        """,
        (
            cuenta_origen_id,
            cuenta_destino_id,
        ),
    )

    filas = cur.fetchall()

    return {
        fila[0]: {
            "id_cuenta": fila[0],
            "saldo": fila[1],
            "divisa": fila[2],
            "estado": fila[3],
        }
        for fila in filas
    }


@medir_sql("debito")
def debitar_cuenta(cur, cuenta_id, monto, divisa):
    """
    Descuenta el monto y retorna el saldo resultante.
    """

    cur.execute(
        """
        UPDATE cuentas
        SET saldo = saldo - %s
        WHERE id_cuenta = %s
          AND estado = 'ACTIVA'
          AND divisa = %s
          AND saldo >= %s
        RETURNING saldo
        """,
        (
            monto,
            cuenta_id,
            divisa,
            monto,
        ),
    )

    fila = cur.fetchone()

    return fila[0] if fila is not None else None


@medir_sql("credito")
def acreditar_cuenta(cur, cuenta_id, monto, divisa):
    """
    Incrementa el saldo y retorna el nuevo saldo.
    """

    cur.execute(
        """
        UPDATE cuentas
        SET saldo = saldo + %s
        WHERE id_cuenta = %s
          AND estado = 'ACTIVA'
          AND divisa = %s
        RETURNING saldo
        """,
        (
            monto,
            cuenta_id,
            divisa,
        ),
    )

    fila = cur.fetchone()

    return fila[0] if fila is not None else None


@medir_sql("insert_movimiento")
def insertar_movimiento(
    cur,
    transaccion_id,
    cuenta_id,
    tipo,
    monto,
    saldo_resultante,
):
    cur.execute(
        """
        INSERT INTO movimientos (
            transaccion_id,
            cuenta_id,
            tipo,
            monto,
            saldo_resultante
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id_movimiento
        """,
        (
            transaccion_id,
            cuenta_id,
            tipo,
            monto,
            saldo_resultante,
        ),
    )

    return cur.fetchone()[0]


@medir_sql("update_transaccion")
def completar_transaccion(
    cur,
    transaccion_id,
):
    cur.execute(
        """
        UPDATE transacciones
        SET
            estado = 'COMPLETADA',
            completado_en = CURRENT_TIMESTAMP
        WHERE id_transaccion = %s
        """,
        (transaccion_id,),
    )


@medir_sql("update_transaccion")
def rechazar_transaccion(
    cur,
    transaccion_id,
    razon,
):
    cur.execute(
        """
        UPDATE transacciones
        SET
            estado = 'RECHAZADA',
            razon_fallo = %s
        WHERE id_transaccion = %s
        """,
        (
            razon,
            transaccion_id,
        ),
    )


@medir_sql("outbox")
def insertar_evento_outbox(
    cur,
    transaccion_id,
    tipo_evento,
    carga_util,
):
    cur.execute(
        """
        INSERT INTO eventos_outbox (
            transaccion_id,
            tipo_evento,
            carga_util
        )
        VALUES (%s, %s, %s::jsonb)
        RETURNING id_eventos
        """,
        (
            transaccion_id,
            tipo_evento,
            carga_util,
        ),
    )

    return cur.fetchone()[0]
