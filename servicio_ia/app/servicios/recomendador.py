from servicio_ia.app.esquemas.analisis import (
    FactoresAnalisis,
    RespuestaAnalisis,
    SolicitudAnalisis,
)


MODELO = "smartbancs-mock-v1"


def calcular_factores(
    solicitud: SolicitudAnalisis,
) -> FactoresAnalisis:

    monto = solicitud.monto
    tipo = solicitud.tipo.strip().upper()
    divisa = solicitud.divisa.strip().upper()

    factor_monto = 0.0
    factor_tipo = 0.0
    factor_divisa = 0.0

    if monto > 1000:
        factor_monto = 0.60
    elif monto >= 100:
        factor_monto = 0.30
    else:
        factor_monto = 0.10

    if tipo == "TRANSFERENCIA":
        factor_tipo = 0.10

    if divisa != "USD":
        factor_divisa = 0.10

    return FactoresAnalisis(
        monto=factor_monto,
        tipo=factor_tipo,
        divisa=factor_divisa,
    )


def calcular_score(
    factores: FactoresAnalisis,
) -> float:

    score = (
        factores.monto
        + factores.tipo
        + factores.divisa
    )

    return round(
        min(score, 1.0),
        2,
    )


def determinar_nivel(
    score: float,
) -> str:

    if score < 0.35:
        return "BAJO"

    if score < 0.70:
        return "MEDIO"

    return "ALTO"


def generar_recomendacion_usuario(
    nivel: str,
) -> str:

    if nivel == "BAJO":
        return (
            "La operación presenta un nivel bajo "
            "de observación."
        )

    if nivel == "MEDIO":
        return (
            "La operación presenta un nivel medio. "
            "Se recomienda revisar sus movimientos recientes."
        )

    return (
        "La operación presenta un nivel alto. "
        "Se recomienda verificar los detalles "
        "de la transacción."
    )


def generar_recomendacion_admin(
    nivel: str,
) -> str:

    if nivel == "BAJO":
        return (
            "Operación con bajo nivel de observación. "
            "No requiere revisión adicional."
        )

    if nivel == "MEDIO":
        return (
            "Se recomienda revisar el patrón "
            "transaccional reciente asociado "
            "a esta operación."
        )

    return (
        "Se recomienda análisis adicional "
        "y revisión de los factores asociados "
        "a la operación."
    )


def analizar_transaccion(
    solicitud: SolicitudAnalisis,
) -> RespuestaAnalisis:

    categoria = (
        solicitud.tipo
        .strip()
        .upper()
    )

    factores = calcular_factores(
        solicitud
    )

    score = calcular_score(
        factores
    )

    nivel = determinar_nivel(
        score
    )

    return RespuestaAnalisis(
        id_transaccion=solicitud.id_transaccion,
        categoria=categoria,
        nivel=nivel,
        score=score,
        factores=factores,
        recomendacion_usuario=(
            generar_recomendacion_usuario(
                nivel
            )
        ),
        recomendacion_admin=(
            generar_recomendacion_admin(
                nivel
            )
        ),
        modelo=MODELO,
    )