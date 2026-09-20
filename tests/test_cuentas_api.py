import uuid

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from servicio_transacciones.app.main import app
from tests.test_api_transacciones import conectar_admin

load_dotenv()

client = TestClient(app)

CAMPOS_PROHIBIDOS = {"id_cuenta", "id_cliente", "saldo", "nombre_titular"}


@pytest.fixture
def cuentas_prueba():
    """Tres cuentas propias (activa, bloqueada, cerrada); se eliminan al terminar."""
    prefijo = f"CTA-{uuid.uuid4().hex[:8]}"
    datos = {
        "activa": (f"{prefijo}-A", "ACTIVA"),
        "bloqueada": (f"{prefijo}-B", "BLOQUEADA"),
        "cerrada": (f"{prefijo}-C", "CERRADA"),
    }
    conn = conectar_admin()
    ids = {}
    try:
        with conn.cursor() as cur:
            for clave, (numero, estado) in datos.items():
                cur.execute(
                    "INSERT INTO cuentas (numero_cuenta, nombre_titular, saldo, estado)"
                    " VALUES (%s, %s, 123.45, %s) RETURNING id_cuenta",
                    (numero, "Titular Privado", estado),
                )
                ids[clave] = str(cur.fetchone()[0])
        conn.commit()
        yield prefijo, datos, ids
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cuentas WHERE numero_cuenta LIKE %s", (prefijo + "-%",))
        conn.commit()
        conn.close()


def test_listar_cuentas_solo_campos_seguros(cuentas_prueba):
    prefijo, datos, _ = cuentas_prueba
    r = client.get("/cuentas", params={"buscar": prefijo})
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["total"] == 3
    assert {c["numero_cuenta"] for c in cuerpo["cuentas"]} == {n for n, _ in datos.values()}
    for c in cuerpo["cuentas"]:
        assert set(c) == {"numero_cuenta", "estado", "divisa"}
        assert not CAMPOS_PROHIBIDOS & set(c)
    assert "Titular Privado" not in r.text and "123.45" not in r.text


def test_buscar_y_solo_activas(cuentas_prueba):
    prefijo, datos, _ = cuentas_prueba
    r = client.get("/cuentas", params={"buscar": prefijo, "solo_activas": "true"})
    assert [c["numero_cuenta"] for c in r.json()["cuentas"]] == [datos["activa"][0]]
    assert r.json()["total"] == 1

    r = client.get("/cuentas", params={"buscar": prefijo + "-B"})
    assert [c["estado"] for c in r.json()["cuentas"]] == ["BLOQUEADA"]


def test_buscar_es_parametrizado_y_trata_comodines_como_texto(cuentas_prueba):
    r = client.get("/cuentas", params={"buscar": "'; DROP TABLE cuentas; --"})
    assert r.status_code == 200 and r.json()["total"] == 0
    r = client.get("/cuentas", params={"buscar": "%"})  # no debe coincidir con todo
    assert all("%" in c["numero_cuenta"] for c in r.json()["cuentas"])
    assert len(client.get("/cuentas", params={"limite": 1}).json()["cuentas"]) <= 1


def test_resolver_cuenta_existente(cuentas_prueba):
    _, datos, ids = cuentas_prueba
    numero = datos["activa"][0]
    r = client.get(f"/cuentas/{numero}/resolver")
    assert r.status_code == 200
    assert r.json() == {"id_cuenta": ids["activa"], "numero_cuenta": numero, "estado": "ACTIVA"}
    assert "saldo" not in r.text and "Titular" not in r.text


def test_resolver_cuenta_inexistente():
    r = client.get(f"/cuentas/NO-EXISTE-{uuid.uuid4().hex}/resolver")
    assert r.status_code == 404


@pytest.mark.parametrize("clave,estado", [("bloqueada", "BLOQUEADA"), ("cerrada", "CERRADA")])
def test_resolver_cuenta_inactiva_devuelve_estado_real(cuentas_prueba, clave, estado):
    _, datos, _ = cuentas_prueba
    r = client.get(f"/cuentas/{datos[clave][0]}/resolver")
    assert r.status_code == 200
    assert r.json()["estado"] == estado


def test_post_transacciones_mantiene_su_contrato(cuentas_prueba):
    _, _, ids = cuentas_prueba
    r = client.post(
        "/transacciones",
        json={
            "id_idempotencia": f"idem-cuentas-{uuid.uuid4().hex}",
            "cuenta_origen_id": ids["activa"],
            "cuenta_destino_id": str(uuid.uuid4()),
            "monto": 10,
            "divisa": "USD",
        },
    )
    assert r.status_code == 404  # destino inexistente, igual que antes
    assert client.post("/transacciones", json={}).status_code == 422
