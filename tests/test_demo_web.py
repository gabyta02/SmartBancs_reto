from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from demo_web import app as demo

ID_TX = "11111111-1111-1111-1111-111111111111"
UUID_ORIGEN = "22222222-2222-2222-2222-222222222222"
UUID_DESTINO = "33333333-3333-3333-3333-333333333333"
CUERPO = {
    "numero_cuenta_origen": "1001",
    "numero_cuenta_destino": "1005",
    "monto": "250.00",
    "divisa": "USD",
    "clave_idempotencia": "k-1",
}
TABLA_CUENTAS = {
    "1001": (UUID_ORIGEN, "ACTIVA"),
    "1005": (UUID_DESTINO, "ACTIVA"),
    "1002": ("44444444-4444-4444-4444-444444444444", "BLOQUEADA"),
}
MENSAJE_CUENTAS = "No se pudo consultar las cuentas. Intente nuevamente."


@pytest.fixture
def cliente():
    return TestClient(demo.app)


def _simular(monkeypatch, manejador_tx=None, cuentas_caidas=False):
    """Simula servicio_transacciones: /cuentas/* con TABLA_CUENTAS; lo demas -> manejador_tx."""
    peticiones = []

    def enrutador(request):
        peticiones.append(request)
        ruta = request.url.path
        if ruta.startswith("/cuentas"):
            if cuentas_caidas:
                return httpx.Response(500, json={"detail": "boom"})
            if ruta == "/cuentas":
                cuentas = [
                    {"numero_cuenta": n, "estado": e, "divisa": "USD", "id_cuenta": u, "saldo": "9"}
                    for n, (u, e) in TABLA_CUENTAS.items()
                    if e == "ACTIVA"
                ]
                return httpx.Response(200, json={"total": len(cuentas), "cuentas": cuentas})
            numero = ruta.split("/")[2]
            if numero not in TABLA_CUENTAS:
                return httpx.Response(404, json={"detail": "Cuenta no encontrada"})
            uuid_, estado = TABLA_CUENTAS[numero]
            return httpx.Response(
                200, json={"id_cuenta": uuid_, "numero_cuenta": numero, "estado": estado}
            )
        if manejador_tx is None:
            pytest.fail("llamada inesperada: " + ruta)
        return manejador_tx(request)

    monkeypatch.setattr(
        demo, "_cliente", lambda: httpx.AsyncClient(transport=httpx.MockTransport(enrutador))
    )
    return peticiones


def _tx_ok(request):
    return httpx.Response(
        201,
        json={
            "id_transaccion": ID_TX,
            "estado": "COMPLETADA",
            "id_idempotencia": "k-1",
            "cuenta_origen_id": UUID_ORIGEN,
            "cuenta_destino_id": UUID_DESTINO,
            "monto": "250.00",
            "divisa": "USD",
        },
    )


def test_pagina_principal(cliente):
    r = cliente.get("/")
    assert r.status_code == 200
    assert "SmartBancs" in r.text
    assert cliente.get("/static/app.js").status_code == 200
    assert "idempotencia" not in r.text.lower()  # no aparece en la interfaz


def test_proxy_transferencia_reenvia_contrato_real_y_traceparent(cliente, monkeypatch):
    visto = {}

    def manejador(request):
        visto["url"] = str(request.url)
        visto["json"] = httpx.Response(200, content=request.read()).json()
        visto["traceparent"] = request.headers["traceparent"]
        return _tx_ok(request)

    _simular(monkeypatch, manejador)
    r = cliente.post("/demo/transacciones", json={**CUERPO, "descripcion": "pago"})

    assert r.status_code == 200
    datos = r.json()
    assert datos["transaccion"]["estado"] == "COMPLETADA"
    # Contrato real hacia el backend: UUID resueltos e id_idempotencia enviada.
    assert visto["json"] == {
        "id_idempotencia": "k-1",
        "cuenta_origen_id": UUID_ORIGEN,
        "cuenta_destino_id": UUID_DESTINO,
        "monto": "250.00",
        "divisa": "USD",
    }
    assert visto["url"].endswith("/transacciones")
    assert len(datos["otel_trace_id"]) == 32
    assert visto["traceparent"].split("-")[1] == datos["otel_trace_id"]
    assert datos["jaeger_trace_url"].endswith("/trace/" + datos["otel_trace_id"])


def test_navegador_nunca_recibe_uuid_ni_clave(cliente, monkeypatch):
    _simular(monkeypatch, _tx_ok)
    r = cliente.post("/demo/transacciones", json=CUERPO)
    assert UUID_ORIGEN not in r.text and UUID_DESTINO not in r.text
    for prohibido in ("cuenta_origen_id", "cuenta_destino_id", "id_idempotencia", "clave_idempotencia", "k-1"):
        assert prohibido not in r.text


def test_resuelve_numero_de_cuenta_por_http(cliente, monkeypatch):
    peticiones = _simular(monkeypatch, _tx_ok)
    assert cliente.post("/demo/transacciones", json=CUERPO).status_code == 200
    rutas = [p.url.path for p in peticiones]
    assert "/cuentas/1001/resolver" in rutas and "/cuentas/1005/resolver" in rutas
    assert rutas.index("/transacciones") > rutas.index("/cuentas/1005/resolver")


def test_clave_idempotencia_se_genera_si_no_llega(cliente, monkeypatch):
    visto = {}

    def manejador(request):
        visto["json"] = httpx.Response(200, content=request.read()).json()
        return _tx_ok(request)

    _simular(monkeypatch, manejador)
    cuerpo = {k: v for k, v in CUERPO.items() if k != "clave_idempotencia"}
    assert cliente.post("/demo/transacciones", json=cuerpo).status_code == 200
    assert len(visto["json"]["id_idempotencia"]) >= 32


def test_proxy_transferencia_propaga_error_de_negocio(cliente, monkeypatch):
    _simular(monkeypatch, lambda req: httpx.Response(404, json={"detail": "Cuenta no encontrada"}))
    r = cliente.post("/demo/transacciones", json=CUERPO)
    assert r.status_code == 404
    assert r.json()["detail"] == "Cuenta no encontrada."


def test_cuenta_inexistente(cliente, monkeypatch):
    _simular(monkeypatch)
    r = cliente.post("/demo/transacciones", json={**CUERPO, "numero_cuenta_destino": "9999"})
    assert r.status_code == 404
    assert r.json()["detail"] == "Cuenta no encontrada."


def test_cuenta_inactiva(cliente, monkeypatch):
    _simular(monkeypatch)
    r = cliente.post("/demo/transacciones", json={**CUERPO, "numero_cuenta_origen": "1002"})
    assert r.status_code == 422
    assert r.json()["detail"] == "Cuenta no disponible para realizar transferencias."


def test_misma_cuenta_origen_y_destino(cliente, monkeypatch):
    _simular(monkeypatch)
    r = cliente.post("/demo/transacciones", json={**CUERPO, "numero_cuenta_destino": "1001"})
    assert r.status_code == 422
    assert r.json()["detail"] == "La cuenta origen y destino deben ser diferentes."


def test_servicio_de_cuentas_con_error(cliente, monkeypatch):
    _simular(monkeypatch, cuentas_caidas=True)
    r = cliente.post("/demo/transacciones", json=CUERPO)
    assert r.status_code == 503
    assert r.json()["detail"] == MENSAJE_CUENTAS
    assert "boom" not in r.text


def test_proxy_transferencia_servicio_no_disponible(cliente, monkeypatch):
    def manejador(request):
        raise httpx.ConnectError("sin conexion")

    _simular(monkeypatch, manejador)
    r = cliente.post("/demo/transacciones", json=CUERPO)
    assert r.status_code == 503
    assert "no disponible" in r.json()["detail"]


def test_demo_lista_cuentas_por_http_sin_datos_sensibles(cliente, monkeypatch):
    peticiones = _simular(monkeypatch)
    r = cliente.get("/demo/cuentas")
    assert r.status_code == 200
    assert [p.url.path for p in peticiones] == ["/cuentas"]
    assert peticiones[0].url.params["solo_activas"] == "true"
    cuentas = r.json()["cuentas"]
    assert {c["numero_cuenta"] for c in cuentas} == {"1001", "1005"}
    assert all(set(c) == {"numero_cuenta", "estado", "divisa"} for c in cuentas)
    assert UUID_ORIGEN not in r.text and "saldo" not in r.text


def test_demo_lista_cuentas_servicio_con_error(cliente, monkeypatch):
    _simular(monkeypatch, cuentas_caidas=True)
    r = cliente.get("/demo/cuentas")
    assert r.status_code == 503
    assert r.json()["detail"] == MENSAJE_CUENTAS


def test_demo_web_no_accede_a_postgresql_directamente():
    base = Path(demo.__file__).parent
    assert "psycopg2" not in (base / "app.py").read_text(encoding="utf-8")
    assert "psycopg2" not in (base / "requirements.txt").read_text(encoding="utf-8")
    assert not hasattr(demo, "psycopg2")


def test_analisis_pendiente_no_es_error(cliente, monkeypatch):
    _simular(monkeypatch, lambda req: httpx.Response(404, json={"detail": "Análisis no encontrado"}))
    r = cliente.get(f"/demo/analisis/{ID_TX}")
    assert r.status_code == 200
    assert r.json()["estado"] == "PENDIENTE"


def test_analisis_disponible(cliente, monkeypatch):
    def manejador(request):
        assert request.url.path == f"/analisis/{ID_TX}/admin"
        return httpx.Response(
            200,
            json={
                "id_transaccion": ID_TX,
                "score": 0.2,
                "nivel": "BAJO",
                "factores": {},
                "recomendacion": "Operación normal",
                "modelo": "smartbancs-mock-v1",
            },
        )

    _simular(monkeypatch, manejador)
    r = cliente.get(f"/demo/analisis/{ID_TX}")
    assert r.json() == {
        "estado": "DISPONIBLE",
        "id_transaccion": ID_TX,
        "nivel": "BAJO",
        "score": 0.2,
        "modelo": "smartbancs-mock-v1",
        "recomendacion": "Operación normal",
    }


def test_analisis_servicio_ia_no_disponible(cliente, monkeypatch):
    def manejador(request):
        raise httpx.ConnectError("sin conexion")

    _simular(monkeypatch, manejador)
    assert cliente.get(f"/demo/analisis/{ID_TX}").status_code == 503


def test_analisis_rechaza_id_invalido(cliente):
    assert cliente.get("/demo/analisis/no-es-uuid").status_code == 422


CSV_VALIDO = (
    "id_transaccion,monto,divisa,fecha,tipo\n"
    "1,100.50,usd,2026-09-19,transferencia\n"
    "2,50,USD,2026-09-19,pago\n"
    "3,,USD,2026-09-19,pago\n"        # monto nulo -> rechazado
    "4,10,USD,fecha-mala,pago\n"      # fecha invalida -> rechazado
)


@pytest.fixture
def etl_real(monkeypatch, tmp_path):
    """Ejecuta el ETL real; solo desvia las salidas a tmp_path para no ensuciar etl/datos.

    Devuelve la lista de rutas de entrada con las que se invoco el ETL.
    """
    import etl.transformar as etl

    original = etl.transformar
    entradas = []

    def envoltorio(archivo_entrada, **_):
        entradas.append(Path(archivo_entrada))
        assert Path(archivo_entrada).exists()
        return original(
            archivo_entrada=archivo_entrada,
            archivo_limpio=tmp_path / "limpio.csv",
            archivo_observado=tmp_path / "observado.csv",
        )

    monkeypatch.setattr(etl, "transformar", envoltorio)
    return entradas


def _subir(cliente, nombre, contenido, campo="archivo"):
    return cliente.post("/demo/etl/ejecutar", files={campo: (nombre, contenido, "text/csv")})


def test_etl_archivo_valido_usa_la_logica_real(cliente, etl_real):
    r = _subir(cliente, "movimientos.csv", CSV_VALIDO)
    assert r.status_code == 200
    d = r.json()
    assert d["estado"] == "COMPLETADO"
    assert d["archivo"] == "movimientos.csv"
    assert d["registros_leidos"] == 4
    assert d["registros_transformados"] == 2
    assert d["registros_cargados"] == 2
    assert d["registros_rechazados"] == 2
    assert "duracion_ms" in d and "errores" not in d
    assert len(etl_real) == 1


def test_etl_no_usa_el_nombre_del_usuario_como_ruta_y_limpia_temporal(cliente, etl_real):
    r = _subir(cliente, "../../etc/passwd.csv", CSV_VALIDO)
    assert r.status_code == 200
    assert r.json()["archivo"] == "passwd.csv"  # solo basename, para mostrar
    ruta = etl_real[0]
    assert ruta.name == "entrada.csv" and "passwd" not in str(ruta)
    assert not ruta.exists() and not ruta.parent.exists()  # temporal eliminado


def test_etl_extension_invalida(cliente, etl_real):
    r = _subir(cliente, "datos.txt", CSV_VALIDO)
    assert r.status_code == 415
    assert r.json()["detalle"] == "Formato de archivo no soportado. Solo se admiten archivos .csv o .xlsx."
    assert etl_real == []


def test_etl_archivo_vacio(cliente, etl_real):
    for contenido in ("", "   \n"):
        r = _subir(cliente, "vacio.csv", contenido)
        assert r.status_code == 400
        assert r.json()["detalle"] == "El archivo está vacío."
    assert etl_real == []


def test_etl_sin_archivo(cliente):
    r = cliente.post("/demo/etl/ejecutar")
    assert r.status_code == 400
    assert r.json()["detalle"] == "Debe seleccionar un archivo."


def test_etl_archivo_demasiado_grande(cliente, monkeypatch, etl_real):
    monkeypatch.setattr(demo, "MAX_MB_ETL", 0)
    r = _subir(cliente, "grande.csv", CSV_VALIDO)
    assert r.status_code == 413
    assert etl_real == []


def test_etl_sin_columnas_requeridas(cliente, etl_real):
    r = _subir(cliente, "malo.csv", "a,b\n1,2\n")
    assert r.status_code == 422
    detalle = r.json()["detalle"]
    assert detalle.startswith("El archivo no contiene las columnas requeridas:")
    assert "id_transaccion" in detalle
    assert not etl_real[0].exists()  # tambien se limpia si el ETL falla


def test_etl_error_de_transformacion_sin_detalles_internos(cliente, monkeypatch):
    import etl.transformar as etl

    rutas = []

    def falla(archivo_entrada, **_):
        rutas.append(Path(archivo_entrada))
        raise RuntimeError("secreto interno /app/etl/x.py")

    monkeypatch.setattr(etl, "transformar", falla)
    r = _subir(cliente, "a.csv", CSV_VALIDO)
    assert r.status_code == 500
    assert r.json()["detalle"] == "Error durante la transformación."
    assert "secreto" not in r.text and "Traceback" not in r.text
    assert not rutas[0].exists()  # temporal eliminado aun con error


def test_etl_error_de_carga(cliente, monkeypatch):
    import etl.transformar as etl

    def falla(archivo_entrada, **_):
        raise PermissionError("/app/etl/datos")

    monkeypatch.setattr(etl, "transformar", falla)
    r = _subir(cliente, "a.csv", CSV_VALIDO)
    assert r.status_code == 500
    assert r.json()["detalle"] == "Error durante la carga."
    assert "/app" not in r.text


def test_etl_csv_mal_formado(cliente, etl_real):
    r = _subir(cliente, "roto.csv", b"\xff\xfe\x00bad\x80\n\x81\x82")
    assert r.status_code == 422
    assert r.json()["detalle"] == "El archivo no tiene un formato CSV válido."


def _xlsx(filas, encabezado=("id_transaccion", "monto", "divisa", "fecha", "tipo")):
    import io

    import openpyxl

    libro = openpyxl.Workbook()
    hoja = libro.active
    if encabezado:
        hoja.append(list(encabezado))
    for fila in filas:
        hoja.append(list(fila))
    buffer = io.BytesIO()
    libro.save(buffer)
    return buffer.getvalue()


def test_etl_excel_xlsx_usa_el_mismo_pipeline(cliente, etl_real):
    import datetime

    contenido = _xlsx([
        ("1", 100.5, "usd", datetime.date(2026, 9, 19), "transferencia"),
        ("2", 50, "USD", "19-09-2026", "pago"),
        ("3", None, "USD", "2026-09-19", "pago"),  # monto vacio -> rechazado
    ])
    r = _subir(cliente, "movimientos.xlsx", contenido)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["archivo"] == "movimientos.xlsx"
    assert (d["registros_leidos"], d["registros_transformados"], d["registros_rechazados"]) == (3, 2, 1)
    assert len(etl_real) == 1 and not etl_real[0].parent.exists()  # temporal eliminado


def test_etl_excel_sin_columnas_requeridas(cliente, etl_real):
    r = _subir(cliente, "malo.xlsx", _xlsx([(1, 2)], encabezado=("a", "b")))
    assert r.status_code == 422
    assert r.json()["detalle"].startswith("El archivo no contiene las columnas requeridas:")


def test_etl_excel_invalido_o_vacio(cliente, etl_real):
    r = _subir(cliente, "roto.xlsx", b"esto no es un excel")
    assert r.status_code == 422
    assert r.json()["detalle"] == "El archivo Excel no es válido."
    r = _subir(cliente, "vacio.xlsx", _xlsx([], encabezado=None))
    assert r.status_code == 400
    assert r.json()["detalle"] == "El archivo está vacío."
    assert etl_real == []


def test_etl_pagina_ofrece_carga_de_archivo(cliente):
    html = cliente.get("/").text
    assert 'type="file"' in html and 'accept=".csv,.xlsx"' in html


def test_estado_dashboard_sin_servicios(cliente, monkeypatch):
    def manejador(request):
        raise httpx.ConnectError("sin conexion")

    _simular(monkeypatch, manejador)
    monkeypatch.setattr(demo, "_tcp_ok", lambda host, puerto: False)
    r = cliente.get("/demo/estado")
    assert r.status_code == 200
    assert not any(r.json().values())
