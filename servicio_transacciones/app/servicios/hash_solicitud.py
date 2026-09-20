import hashlib
from decimal import Decimal
from uuid import UUID


def generar_hash_solicitud(
    cuenta_origen_id: UUID,
    cuenta_destino_id: UUID,
    monto: Decimal,
    divisa: str,
) -> str:
    contenido = (
        f"{UUID(str(cuenta_origen_id))}|"
        f"{UUID(str(cuenta_destino_id))}|"
        f"{Decimal(str(monto)).quantize(Decimal('0.01'))}|"
        f"{divisa.upper()}"
    )

    return hashlib.sha256(
        contenido.encode("utf-8")
    ).hexdigest()

