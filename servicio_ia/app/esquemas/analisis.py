from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class SolicitudAnalisis(BaseModel):
    id_transaccion: UUID
    monto: Decimal = Field(gt=0)
    divisa: str
    tipo: str


class FactoresAnalisis(BaseModel):
    monto: float
    tipo: float
    divisa: float


class RespuestaAnalisis(BaseModel):
    id_transaccion: UUID
    categoria: str
    nivel: str
    score: float
    factores: FactoresAnalisis
    recomendacion_usuario: str
    recomendacion_admin: str
    modelo: str

class AnalisisUsuarioResponse(BaseModel):
    id_transaccion: UUID
    nivel: str
    recomendacion: str


class AnalisisAdminResponse(BaseModel):
    id_transaccion: UUID
    score: float
    nivel: str
    factores: dict[str, Any]
    recomendacion: str
    modelo: str
