from uuid import UUID

from pydantic import BaseModel


class CuentaPublica(BaseModel):
    """Vista segura para interfaces: sin UUID, saldo ni datos del titular."""

    numero_cuenta: str
    estado: str
    divisa: str


class ListaCuentasResponse(BaseModel):
    total: int
    cuentas: list[CuentaPublica]


class CuentaResuelta(BaseModel):
    """Uso interno servicio a servicio (demo_web); no se entrega al navegador."""

    id_cuenta: UUID
    numero_cuenta: str
    estado: str
