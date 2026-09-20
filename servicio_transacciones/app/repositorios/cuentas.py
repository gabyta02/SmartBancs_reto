from typing import Optional

from psycopg2.extensions import cursor


def listar_cuentas(
    cur: cursor,
    patron: Optional[str],
    solo_activas: bool,
    limite: int,
) -> tuple[int, list[dict]]:
    """Solo lectura. `patron` es un LIKE ya escapado; siempre va parametrizado."""
    cur.execute(
        """
        SELECT numero_cuenta, estado, divisa, COUNT(*) OVER () AS total
        FROM cuentas
        WHERE (%(patron)s::text IS NULL OR numero_cuenta ILIKE %(patron)s::text)
          AND (NOT %(solo_activas)s OR estado = 'ACTIVA')
        ORDER BY numero_cuenta
        LIMIT %(limite)s
        """,
        {"patron": patron, "solo_activas": solo_activas, "limite": limite},
    )
    filas = cur.fetchall()
    total = filas[0][3] if filas else 0
    return total, [
        {"numero_cuenta": f[0], "estado": f[1], "divisa": f[2]} for f in filas
    ]


def obtener_cuenta_por_numero(cur: cursor, numero_cuenta: str) -> Optional[dict]:
    """Solo lectura: identificador interno y estado. No lee saldo ni titular."""
    cur.execute(
        """
        SELECT id_cuenta, numero_cuenta, estado
        FROM cuentas
        WHERE numero_cuenta = %s
        """,
        (numero_cuenta,),
    )
    fila = cur.fetchone()
    if fila is None:
        return None
    return {"id_cuenta": fila[0], "numero_cuenta": fila[1], "estado": fila[2]}
