from uuid import UUID

from fastapi import (
    APIRouter,
    HTTPException,
    status,
)

from servicio_ia.app.database.conexion import (
    crear_conexion,
)

from servicio_ia.app.esquemas.analisis import (
    AnalisisAdminResponse,
    AnalisisUsuarioResponse,
)

from servicio_ia.app.repositorios.analisis import (
    obtener_analisis_por_transaccion,
)


router = APIRouter(
    prefix="/analisis",
    tags=["Análisis IA"],
)


@router.get(
    "/{transaccion_id}/usuario",
    response_model=AnalisisUsuarioResponse,
)
def obtener_analisis_usuario(
    transaccion_id: UUID,
):
    conn = None

    try:
        conn = crear_conexion()

        analisis = obtener_analisis_por_transaccion(
            conn,
            transaccion_id,
        )

        if analisis is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Análisis no encontrado",
            )

        return AnalisisUsuarioResponse(
            id_transaccion=analisis[
                "transaccion_id"
            ],
            nivel=analisis["nivel"],
            recomendacion=analisis[
                "recomendacion_usuario"
            ],
        )

    finally:
        if conn is not None:
            conn.close()


@router.get(
    "/{transaccion_id}/admin",
    response_model=AnalisisAdminResponse,
)
def obtener_analisis_admin(
    transaccion_id: UUID,
):
    conn = None

    try:
        conn = crear_conexion()

        analisis = obtener_analisis_por_transaccion(
            conn,
            transaccion_id,
        )

        if analisis is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Análisis no encontrado",
            )

        return AnalisisAdminResponse(
            id_transaccion=analisis[
                "transaccion_id"
            ],
            score=analisis["score"],
            nivel=analisis["nivel"],
            factores=analisis["factores"],
            recomendacion=analisis[
                "recomendacion_admin"
            ],
            modelo=analisis["modelo"],
        )

    finally:
        if conn is not None:
            conn.close()