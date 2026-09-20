from unittest.mock import patch

from fastapi.testclient import TestClient

from servicio_ia.app.main import app


client = TestClient(app)


TRANSACCION_ID = (
    "11111111-1111-1111-1111-111111111111"
)


ANALISIS_MOCK = {
    "transaccion_id": TRANSACCION_ID,
    "score": 0.20,
    "nivel": "BAJO",
    "factores": {
        "monto": 0.10,
        "tipo": 0.10,
        "divisa": 0.00,
    },
    "recomendacion_usuario": (
        "La operación presenta un nivel bajo "
        "de observación."
    ),
    "recomendacion_admin": (
        "Operación con bajo nivel de observación. "
        "No requiere revisión adicional."
    ),
    "modelo": "smartbancs-mock-v1",
}


@patch(
    "servicio_ia.app.rutas.analisis."
    "obtener_analisis_por_transaccion"
)
@patch(
    "servicio_ia.app.rutas.analisis."
    "crear_conexion"
)
def test_obtener_analisis_usuario(
    mock_conexion,
    mock_obtener,
):
    mock_obtener.return_value = ANALISIS_MOCK

    response = client.get(
        f"/analisis/{TRANSACCION_ID}/usuario"
    )

    assert response.status_code == 200

    data = response.json()

    assert data["id_transaccion"] == TRANSACCION_ID
    assert data["nivel"] == "BAJO"
    assert data["recomendacion"] == ANALISIS_MOCK["recomendacion_usuario"]

    assert "score" not in data
    assert "factores" not in data
    assert "modelo" not in data


@patch(
    "servicio_ia.app.rutas.analisis."
    "obtener_analisis_por_transaccion"
)
@patch(
    "servicio_ia.app.rutas.analisis."
    "crear_conexion"
)
def test_obtener_analisis_admin(
    mock_conexion,
    mock_obtener,
):
    mock_obtener.return_value = ANALISIS_MOCK

    response = client.get(
        f"/analisis/{TRANSACCION_ID}/admin"
    )

    assert response.status_code == 200

    data = response.json()

    assert data["id_transaccion"] == TRANSACCION_ID
    assert data["score"] == 0.20
    assert data["nivel"] == "BAJO"

    assert data["factores"]["monto"] == 0.10
    assert data["factores"]["tipo"] == 0.10
    assert data["factores"]["divisa"] == 0.00

    assert data["modelo"] == "smartbancs-mock-v1"

    assert data["recomendacion"] == ANALISIS_MOCK["recomendacion_admin"]


@patch(
    "servicio_ia.app.rutas.analisis."
    "obtener_analisis_por_transaccion"
)
@patch(
    "servicio_ia.app.rutas.analisis."
    "crear_conexion"
)
def test_analisis_usuario_no_encontrado(
    mock_conexion,
    mock_obtener,
):
    mock_obtener.return_value = None

    response = client.get(
        f"/analisis/{TRANSACCION_ID}/usuario"
    )

    assert response.status_code == 404

    assert response.json() == {
        "detail": "Análisis no encontrado"
    }


@patch(
    "servicio_ia.app.rutas.analisis."
    "obtener_analisis_por_transaccion"
)
@patch(
    "servicio_ia.app.rutas.analisis."
    "crear_conexion"
)
def test_analisis_admin_no_encontrado(
    mock_conexion,
    mock_obtener,
):
    mock_obtener.return_value = None

    response = client.get(
        f"/analisis/{TRANSACCION_ID}/admin"
    )

    assert response.status_code == 404

    assert response.json() == {
        "detail": "Análisis no encontrado"
    }
