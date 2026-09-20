from fastapi.testclient import TestClient

from servicio_ia.app.main import app


client = TestClient(app)


def test_health_ia():
    response = client.get(
        "/health"
    )

    assert response.status_code == 200

    data = response.json()

    assert data["status"] == "ok"
    assert data["service"] == "ia"
    assert data["modelo"] == "smartbancs-mock-v1"


def test_ia_nivel_bajo():
    response = client.post(
        "/analizar",
        json={
            "id_transaccion":
                "11111111-1111-1111-1111-111111111111",
            "monto": 50,
            "divisa": "USD",
            "tipo": "transferencia",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["nivel"] == "BAJO"
    assert data["score"] == 0.20
    assert data["categoria"] == "TRANSFERENCIA"

    assert data["factores"]["monto"] == 0.10
    assert data["factores"]["tipo"] == 0.10
    assert data["factores"]["divisa"] == 0.0

    assert data["modelo"] == "smartbancs-mock-v1"

    assert data["recomendacion_usuario"]
    assert data["recomendacion_admin"]


def test_ia_nivel_medio():
    response = client.post(
        "/analizar",
        json={
            "id_transaccion":
                "22222222-2222-2222-2222-222222222222",
            "monto": 500,
            "divisa": "USD",
            "tipo": "transferencia",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["score"] == 0.40
    assert data["nivel"] == "MEDIO"

    assert data["factores"]["monto"] == 0.30
    assert data["factores"]["tipo"] == 0.10
    assert data["factores"]["divisa"] == 0.0


def test_ia_nivel_alto():
    response = client.post(
        "/analizar",
        json={
            "id_transaccion":
                "33333333-3333-3333-3333-333333333333",
            "monto": 1500,
            "divisa": "EUR",
            "tipo": "transferencia",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["score"] == 0.80
    assert data["nivel"] == "ALTO"

    assert data["factores"]["monto"] == 0.60
    assert data["factores"]["tipo"] == 0.10
    assert data["factores"]["divisa"] == 0.10

    assert data["modelo"] == "smartbancs-mock-v1"

    assert data["recomendacion_usuario"]
    assert data["recomendacion_admin"]


def test_ia_divisa_distinta_incrementa_score():
    response = client.post(
        "/analizar",
        json={
            "id_transaccion":
                "44444444-4444-4444-4444-444444444444",
            "monto": 50,
            "divisa": "EUR",
            "tipo": "pago",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["score"] == 0.20
    assert data["nivel"] == "BAJO"

    assert data["factores"]["monto"] == 0.10
    assert data["factores"]["tipo"] == 0.0
    assert data["factores"]["divisa"] == 0.10


def test_ia_normaliza_tipo():
    response = client.post(
        "/analizar",
        json={
            "id_transaccion":
                "55555555-5555-5555-5555-555555555555",
            "monto": 300,
            "divisa": "USD",
            "tipo": " transferencia ",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["categoria"] == "TRANSFERENCIA"


def test_ia_rechaza_monto_negativo():
    response = client.post(
        "/analizar",
        json={
            "id_transaccion":
                "66666666-6666-6666-6666-666666666666",
            "monto": -20,
            "divisa": "USD",
            "tipo": "transferencia",
        },
    )

    assert response.status_code == 422


def test_ia_rechaza_monto_cero():
    response = client.post(
        "/analizar",
        json={
            "id_transaccion":
                "77777777-7777-7777-7777-777777777777",
            "monto": 0,
            "divisa": "USD",
            "tipo": "transferencia",
        },
    )

    assert response.status_code == 422


def test_ia_rechaza_monto_invalido():
    response = client.post(
        "/analizar",
        json={
            "id_transaccion":
                "88888888-8888-8888-8888-888888888888",
            "monto": "abc",
            "divisa": "USD",
            "tipo": "transferencia",
        },
    )

    assert response.status_code == 422


def test_ia_rechaza_uuid_invalido():
    response = client.post(
        "/analizar",
        json={
            "id_transaccion": "no-es-un-uuid",
            "monto": 100,
            "divisa": "USD",
            "tipo": "transferencia",
        },
    )

    assert response.status_code == 422