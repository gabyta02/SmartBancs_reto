from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class TransferenciaCreate(BaseModel):
    id_idempotencia: str = Field(min_length=1, max_length=100)
    cuenta_origen_id: UUID
    cuenta_destino_id: UUID
    monto: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    divisa: str = Field(default="USD", min_length=3, max_length=3)

    @field_validator("divisa")
    @classmethod
    def normalizar_divisa(cls, valor: str) -> str:
        return valor.upper()

    @field_validator("cuenta_destino_id")
    @classmethod
    def validar_cuentas_diferentes(cls, cuenta_destino_id: UUID, info) -> UUID:
        if info.data.get("cuenta_origen_id") == cuenta_destino_id:
            raise ValueError("La cuenta origen y destino deben ser diferentes")
        return cuenta_destino_id


class TransferenciaResponse(BaseModel):
    id_transaccion: UUID
    id_idempotencia: str
    cuenta_origen_id: UUID
    cuenta_destino_id: UUID
    monto: Decimal
    divisa: str
    estado: str
    razon_fallo: str | None = None
    creado_en: datetime
    reproducida: bool = False  # True si se devolvio un resultado previo por idempotencia
