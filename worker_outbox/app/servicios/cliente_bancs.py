import httpx

from worker_outbox.app.configuracion import settings


def enviar_evento_bancs(
    evento: dict,
) -> None:
    payload = {
        "id_evento": str(
            evento["id_eventos"]
        ),
        "transaccion_id": str(
            evento["transaccion_id"]
        ),
        "tipo_evento": evento["tipo_evento"],
        "carga_util": evento["carga_util"],
    }

    with httpx.Client(
        timeout=2.0,
    ) as client:
        response = client.post(
            f"{settings.bancs_url}/eventos",
            json=payload,
        )

        response.raise_for_status()