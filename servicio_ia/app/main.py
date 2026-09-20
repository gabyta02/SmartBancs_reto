from fastapi import FastAPI

from servicio_ia.app.esquemas.analisis import (
    RespuestaAnalisis,
    SolicitudAnalisis,
)

from servicio_ia.app.rutas.analisis import (
    router as analisis_router,
)

from servicio_ia.app.servicios.recomendador import (
    analizar_transaccion,
)


app = FastAPI(
    title="SmartBancs - Servicio IA",
    version="1.0.0",
)


app.include_router(
    analisis_router
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "ia",
        "modelo": "smartbancs-mock-v1",
    }


@app.post(
    "/analizar",
    response_model=RespuestaAnalisis,
)
def analizar(
    solicitud: SolicitudAnalisis,
):
    return analizar_transaccion(
        solicitud
    )