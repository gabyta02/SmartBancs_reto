from typing import Optional

from servicio_transacciones.app.repositorios import cuentas as repo


class CuentaNoExiste(Exception):
    pass


def _patron_busqueda(buscar: Optional[str]) -> Optional[str]:
    """Busqueda por contenido; escapa los comodines de LIKE para tratarlos como texto."""
    buscar = (buscar or "").strip()
    if not buscar:
        return None
    escapado = buscar.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escapado}%"


def listar(conn, buscar: Optional[str], solo_activas: bool, limite: int):
    with conn.cursor() as cur:
        return repo.listar_cuentas(cur, _patron_busqueda(buscar), solo_activas, limite)


def resolver(conn, numero_cuenta: str) -> dict:
    with conn.cursor() as cur:
        cuenta = repo.obtener_cuenta_por_numero(cur, numero_cuenta)
    if cuenta is None:
        raise CuentaNoExiste(numero_cuenta)
    return cuenta
