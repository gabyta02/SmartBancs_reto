from psycopg2 import errors

from servicio_transacciones.app.repositorios.transacciones import (
    crear_transaccion_idempotente,
    obtener_transaccion_por_idempotencia,
)


class ConflictoIdempotencia(Exception):
    """
    Se lanza cuando una clave de idempotencia
    ya existe pero corresponde a una solicitud distinta.
    """

    pass

class ConflictoIdempotencia(Exception):
    pass



def validar_idempotencia(
    transaccion_existente: dict | None,
    hash_solicitud: str,
):
    """
    Valida el estado de una solicitud idempotente.

    Retorna:
        None si la clave no existe y puede procesarse.

        dict si ya existe exactamente la misma solicitud,
        para reutilizar el resultado previo.

    Lanza:
        ConflictoIdempotencia si la misma clave fue usada
        con una solicitud diferente.
    """

    if transaccion_existente is None:
        return None

    hash_existente = transaccion_existente[
        "hash_solicitud"
    ].strip()

    if hash_existente == hash_solicitud:
        return transaccion_existente

    raise ConflictoIdempotencia(
        "La clave de idempotencia ya fue utilizada "
        "con una solicitud diferente."
    )


def validar_idempotencia(
    transaccion_existente,
    hash_solicitud,
):
    if transaccion_existente is None:
        return None

    hash_existente = (
        transaccion_existente[
            "hash_solicitud"
        ].strip()
    )

    if hash_existente == hash_solicitud:
        return transaccion_existente

    raise ConflictoIdempotencia(
        "La clave de idempotencia ya fue utilizada "
        "con una solicitud diferente."
    )


def obtener_o_crear_transaccion(
    conn,
    id_idempotencia,
    hash_solicitud,
    cuenta_origen_id,
    cuenta_destino_id,
    monto,
    divisa="USD",
):
    """
    Crea una transacción de manera segura frente
    a solicitudes concurrentes con la misma
    clave de idempotencia.

    Retorna:
        {
            "creada": True/False,
            "transaccion": ...
        }
    """

    with conn.cursor() as cur:

        existente = (
            obtener_transaccion_por_idempotencia(
                cur,
                id_idempotencia,
            )
        )

        resultado = validar_idempotencia(
            existente,
            hash_solicitud,
        )

        if resultado is not None:
            return {
                "creada": False,
                "transaccion": resultado,
            }

        cur.execute("SAVEPOINT sp_idempotencia")

        try:
            transaccion_id, creado_en = (
                crear_transaccion_idempotente(
                    cur,
                    id_idempotencia,
                    hash_solicitud,
                    cuenta_origen_id,
                    cuenta_destino_id,
                    monto,
                    divisa,
                )
            )

            cur.execute(
                "RELEASE SAVEPOINT sp_idempotencia"
            )

            return {
                "creada": True,
                "transaccion": {
                    "id_transaccion":
                        transaccion_id,
                    "id_idempotencia":
                        id_idempotencia,
                    "hash_solicitud":
                        hash_solicitud,
                    "estado":
                        "PENDIENTE",
                    "creado_en":
                        creado_en,
                },
            }

        except errors.UniqueViolation:

            cur.execute(
                "ROLLBACK TO SAVEPOINT sp_idempotencia"
            )

            existente = (
                obtener_transaccion_por_idempotencia(
                    cur,
                    id_idempotencia,
                )
            )

            resultado = validar_idempotencia(
                existente,
                hash_solicitud,
            )

            if resultado is None:
                raise RuntimeError(
                    "La idempotencia entró en conflicto "
                    "pero la transacción no pudo recuperarse."
                )

            return {
                "creada": False,
                "transaccion": resultado,
            }