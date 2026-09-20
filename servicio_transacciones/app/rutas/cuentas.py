from fastapi import APIRouter, Depends, HTTPException, Query, status

from servicio_transacciones.app.database.conexion import obtener_conexion
from servicio_transacciones.app.esquemas.cuentas import (
    CuentaResuelta,
    ListaCuentasResponse,
)
from servicio_transacciones.app.servicios import cuentas as servicio


router = APIRouter(
    prefix="/cuentas",
    tags=["Cuentas"],
)


@router.get("", response_model=ListaCuentasResponse)
def listar_cuentas(
    buscar: str | None = Query(default=None, max_length=30),
    solo_activas: bool = False,
    limite: int = Query(default=50, ge=1, le=200),
    conn=Depends(obtener_conexion),
):
    total, cuentas = servicio.listar(conn, buscar, solo_activas, limite)
    return {"total": total, "cuentas": cuentas}


@router.get("/{numero_cuenta}/resolver", response_model=CuentaResuelta)
def resolver_cuenta(
    numero_cuenta: str,
    conn=Depends(obtener_conexion),
):
    try:
        return servicio.resolver(conn, numero_cuenta)
    except servicio.CuentaNoExiste as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cuenta no encontrada",
        ) from exc
