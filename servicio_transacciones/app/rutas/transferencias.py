import time

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    status,
)

from servicio_transacciones.app.observabilidad.metricas import (
    RUTA_TRANSACCION_DURACION_SECONDS,
    THREADPOOL_QUEUE_SECONDS,
    registrar_error_http,
    worker_actual,
)

from servicio_transacciones.app.esquemas.transacciones import (
    TransferenciaCreate,
    TransferenciaResponse,
)

from servicio_transacciones.app.servicios.hash_solicitud import (
    generar_hash_solicitud,
)

from servicio_transacciones.app.servicios.idempotencia import (
    ConflictoIdempotencia,
)

from servicio_transacciones.app.servicios.transferencias import (
    CuentaNoEncontrada,
    ServicioSaturado,
    procesar_transferencia,
)


router = APIRouter(
    prefix="/transacciones",
    tags=["Transacciones"],
)


@router.post(
    "",
    response_model=TransferenciaResponse,
    status_code=status.HTTP_201_CREATED,
)
def crear_transferencia(
    datos: TransferenciaCreate,
    request: Request,
):
    inicio_ruta = time.perf_counter()
    worker = worker_actual()
    inicio_request = request.scope.get("smartbancs_request_inicio")
    if inicio_request is not None:
        THREADPOOL_QUEUE_SECONDS.labels(worker=worker).observe(
            max(0, inicio_ruta - inicio_request)
        )
    try:
        hash_solicitud = generar_hash_solicitud(
            cuenta_origen_id=datos.cuenta_origen_id,
            cuenta_destino_id=datos.cuenta_destino_id,
            monto=datos.monto,
            divisa=datos.divisa,
        )

        resultado = procesar_transferencia(
            id_idempotencia=datos.id_idempotencia,
            hash_solicitud=hash_solicitud,
            cuenta_origen_id=datos.cuenta_origen_id,
            cuenta_destino_id=datos.cuenta_destino_id,
            monto=datos.monto,
            divisa=datos.divisa,
        )

        return resultado

    except ConflictoIdempotencia as exc:
        registrar_error_http(409, "negocio", "conflicto_idempotencia")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    except CuentaNoEncontrada as exc:
        registrar_error_http(404, "negocio", "cuenta_no_encontrada")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    except ServicioSaturado as exc:
        motivo = str(exc)
        registrar_error_http(
            503, "servicio_saturado",
            motivo if motivo in {"57014", "55P03", "40P01"} else "otro",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servicio temporalmente saturado. Reintente con la misma clave de idempotencia.",
            headers={"Retry-After": "1"},
        ) from exc
    finally:
        RUTA_TRANSACCION_DURACION_SECONDS.labels(worker=worker).observe(
            time.perf_counter() - inicio_ruta
        )
