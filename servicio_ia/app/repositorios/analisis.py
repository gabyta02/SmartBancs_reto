from psycopg2.extras import Json

from servicio_ia.app.esquemas.analisis import (
    RespuestaAnalisis,
)


def guardar_analisis(
    conn,
    resultado: RespuestaAnalisis,
) -> None:

    consulta = """
        INSERT INTO analisis_ia (
            transaccion_id,
            score,
            nivel,
            factores,
            recomen_user,
            recomen_admin,
            modelo
        )
        VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s
        )
        ON CONFLICT (transaccion_id)
        DO UPDATE SET
            score = EXCLUDED.score,
            nivel = EXCLUDED.nivel,
            factores = EXCLUDED.factores,
            recomen_user=
                EXCLUDED.recomen_user,
            recomen_admin =
                EXCLUDED.recomen_admin,
            modelo = EXCLUDED.modelo;
    """

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                consulta,
                (
                    str(
                        resultado.id_transaccion
                    ),
                    resultado.score,
                    resultado.nivel,
                    Json(
                        resultado.factores.model_dump()
                    ),
                    resultado.recomendacion_usuario,
                    resultado.recomendacion_admin,
                    resultado.modelo,
                ),
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

def obtener_analisis_por_transaccion(
    conn,
    transaccion_id,
):
    consulta = """
        SELECT
            transaccion_id,
            score,
            nivel,
            factores,
            recomen_user AS recomendacion_usuario,
            recomen_admin AS recomendacion_admin,
            modelo,
            creado_en
        FROM analisis_ia
        WHERE transaccion_id = %s;
    """

    with conn.cursor() as cursor:
        cursor.execute(
            consulta,
            (
                str(transaccion_id),
            ),
        )

        fila = cursor.fetchone()

    if fila is None:
        return None

    return {
        "transaccion_id": fila[0],
        "score": float(fila[1]),
        "nivel": fila[2],
        "factores": fila[3],
        "recomendacion_usuario": fila[4],
        "recomendacion_admin": fila[5],
        "modelo": fila[6],
        "creado_en": fila[7],
    }
