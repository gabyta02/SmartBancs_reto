from fastapi import FastAPI
from pydantic import BaseModel


app = FastAPI(
    title="SmartBancs - Core Bancs Mock",
    version="1.0.0",
)


class EventoBancs(BaseModel):
    id_evento: str
    transaccion_id: str
    tipo_evento: str
    carga_util: dict


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "bancs",
    }


@app.post("/eventos")
def recibir_evento(
    evento: EventoBancs,
):
    return {
        "procesado": True,
        "id_evento": evento.id_evento,
        "transaccion_id": evento.transaccion_id,
    }