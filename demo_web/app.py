"""SmartBancs Demo: fachada minima para la interfaz web de demostracion.

No contiene logica de negocio: reenvia a los servicios existentes,
consulta el analisis IA ya persistido y ejecuta el ETL conocido.
"""
import asyncio
import logging
import os
import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, File, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import pandas as pd
from pandas import errors as pd_errors
from pydantic import BaseModel, Field

URL_TRANSACCIONES = os.getenv("URL_TRANSACCIONES", "http://servicio_transacciones:8000")
URL_IA = os.getenv("URL_IA", "http://servicio_ia:8002")
URL_BANCS = os.getenv("URL_BANCS", "http://servicio_bancs:8001")
URL_WORKER_METRICAS = os.getenv("URL_WORKER_METRICAS", "http://worker_outbox:9101/metrics")
URL_CONSUMIDOR_METRICAS = os.getenv("URL_CONSUMIDOR_METRICAS", "http://consumidor_ia:9102/metrics")
HOST_POSTGRES = os.getenv("HOST_POSTGRES", "postgres")
PUERTO_POSTGRES = int(os.getenv("PUERTO_POSTGRES", "5432"))
HOST_RABBITMQ = os.getenv("HOST_RABBITMQ", "rabbitmq")
PUERTO_RABBITMQ = int(os.getenv("PUERTO_RABBITMQ", "5672"))

# Enlaces que abre el navegador del usuario (no el hostname Docker).
URL_GRAFANA = os.getenv("URL_GRAFANA", "http://localhost:3000")
URL_JAEGER = os.getenv("URL_JAEGER", "http://localhost:18086")

# Cuentas por defecto (numeros de cuenta) para el formulario; opcional.
CUENTA_ORIGEN_DEMO = os.getenv("DEMO_CUENTA_ORIGEN", "")
CUENTA_DESTINO_DEMO = os.getenv("DEMO_CUENTA_DESTINO", "")

STATIC_DIR = Path(__file__).resolve().parent / "static"

logger = logging.getLogger("demo_web")

app = FastAPI(title="SmartBancs Demo", version="1.0.0")


def _cliente() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0)


class TransferenciaDemo(BaseModel):
    numero_cuenta_origen: str = Field(min_length=1, max_length=30)
    numero_cuenta_destino: str = Field(min_length=1, max_length=30)
    monto: str | int | float
    divisa: str = "USD"
    # El contrato de POST /transacciones no tiene descripcion: se acepta pero no se reenvia.
    descripcion: str | None = Field(default=None, max_length=255)
    # Tecnica e interna: el frontend la genera y no se muestra al usuario.
    # Si no llega, se genera aqui. El contrato del backend la llama id_idempotencia.
    clave_idempotencia: str = Field(default_factory=lambda: str(uuid4()), max_length=100)


class ErrorServicioCuentas(Exception):
    pass


async def _resolver_cuenta(numero: str):
    """Numero de cuenta -> (UUID, estado) via GET /cuentas/{numero}/resolver.

    El UUID solo circula entre demo_web y servicio_transacciones.
    """
    try:
        async with _cliente() as c:
            r = await c.get(f"{URL_TRANSACCIONES}/cuentas/{quote(numero, safe='')}/resolver")
    except httpx.HTTPError as exc:
        raise ErrorServicioCuentas() from exc
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise ErrorServicioCuentas()
    datos = r.json()
    return datos["id_cuenta"], datos["estado"]


@app.get("/")
def inicio():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/demo/config")
def config():
    return {
        "grafana_url": URL_GRAFANA,
        "jaeger_url": URL_JAEGER,
        "cuenta_origen": CUENTA_ORIGEN_DEMO,
        "cuenta_destino": CUENTA_DESTINO_DEMO,
    }


# ---------------------------------------------------------------- Dashboard

async def _http_ok(url: str) -> bool:
    try:
        async with _cliente() as c:
            r = await c.get(url)
        return r.status_code < 500
    except httpx.HTTPError:
        return False


def _tcp_ok(host: str, puerto: int) -> bool:
    try:
        with socket.create_connection((host, puerto), timeout=2):
            return True
    except OSError:
        return False


@app.get("/demo/estado")
async def estado():
    """Estado basico: /health donde existe, TCP o /metrics en el resto."""
    (api, ia, bancs, worker, consumidor, pg, rabbit) = await asyncio.gather(
        _http_ok(f"{URL_TRANSACCIONES}/health"),
        _http_ok(f"{URL_IA}/health"),
        _http_ok(f"{URL_BANCS}/docs"),
        _http_ok(URL_WORKER_METRICAS),
        _http_ok(URL_CONSUMIDOR_METRICAS),
        asyncio.to_thread(_tcp_ok, HOST_POSTGRES, PUERTO_POSTGRES),
        asyncio.to_thread(_tcp_ok, HOST_RABBITMQ, PUERTO_RABBITMQ),
    )
    return {
        "api_transacciones": api,
        "postgresql": pg,
        "worker_outbox": worker,
        "rabbitmq": rabbit,
        "consumidor_ia": consumidor,
        "servicio_ia": ia,
        "servicio_bancs": bancs,
    }


# ------------------------------------------------------------ Transferencias

MENSAJE_ERROR_CUENTAS = "No se pudo consultar las cuentas. Intente nuevamente."


@app.get("/demo/cuentas")
async def cuentas(
    buscar: str | None = Query(default=None, max_length=30),
    limite: int = Query(default=200, ge=1, le=200),
):
    """Listado seguro (numero, estado, divisa) para los selectores. Solo cuentas activas."""
    params = {"solo_activas": "true", "limite": limite}
    if buscar:
        params["buscar"] = buscar
    try:
        async with _cliente() as c:
            r = await c.get(f"{URL_TRANSACCIONES}/cuentas", params=params)
    except httpx.HTTPError:
        return JSONResponse(status_code=503, content={"detail": MENSAJE_ERROR_CUENTAS})
    if r.status_code != 200:
        return JSONResponse(status_code=503, content={"detail": MENSAJE_ERROR_CUENTAS})
    datos = r.json()
    # Solo campos seguros, aunque el servicio agregara otros en el futuro.
    return {
        "total": datos["total"],
        "cuentas": [
            {"numero_cuenta": c["numero_cuenta"], "estado": c["estado"], "divisa": c["divisa"]}
            for c in datos["cuentas"]
        ],
    }


def _error(status: int, detalle: str, **extra):
    return JSONResponse(status_code=status, content={"detail": detalle, **extra})


@app.post("/demo/transacciones")
async def transferir(datos: TransferenciaDemo):
    origen_num = datos.numero_cuenta_origen.strip()
    destino_num = datos.numero_cuenta_destino.strip()
    if origen_num == destino_num:
        return _error(422, "La cuenta origen y destino deben ser diferentes.")

    # 1) Resolver numero de cuenta -> UUID interno (nunca se devuelve al navegador).
    try:
        origen = await _resolver_cuenta(origen_num)
        destino = await _resolver_cuenta(destino_num)
    except ErrorServicioCuentas:
        return _error(503, MENSAJE_ERROR_CUENTAS)
    if origen is None or destino is None:
        return _error(404, "Cuenta no encontrada.")
    if origen[1] != "ACTIVA" or destino[1] != "ACTIVA":
        return _error(422, "Cuenta no disponible para realizar transferencias.")

    # traceparent W3C propio: el servicio lo adopta como Trace ID OpenTelemetry y
    # worker_outbox / consumidor_ia ya propagan ese mismo contexto. Asi conocemos el
    # Trace ID sin modificar ningun servicio.
    otel_trace_id = secrets.token_hex(16)
    traceparent = f"00-{otel_trace_id}-{secrets.token_hex(8)}-01"

    cuerpo_interno = {
        "id_idempotencia": datos.clave_idempotencia,
        "cuenta_origen_id": origen[0],
        "cuenta_destino_id": destino[0],
        "monto": datos.monto,
        "divisa": datos.divisa,
    }

    inicio = time.perf_counter()
    try:
        async with _cliente() as c:
            r = await c.post(
                f"{URL_TRANSACCIONES}/transacciones",
                json=cuerpo_interno,
                headers={"traceparent": traceparent},
            )
    except httpx.HTTPError:
        return _error(503, "Servicio de transacciones no disponible")
    tiempo_ms = round((time.perf_counter() - inicio) * 1000)

    try:
        cuerpo = r.json()
    except ValueError:
        cuerpo = {"detail": r.text}

    if r.status_code >= 400:
        if r.status_code == 404:
            return _error(404, "Cuenta no encontrada.", tiempo_ms=tiempo_ms)
        if r.status_code == 422:
            # Validacion de campos (monto, divisa...): sin detalles internos ni UUID.
            return _error(422, "Datos de transferencia invalidos.", tiempo_ms=tiempo_ms)
        detalle = cuerpo.get("detail", "Error al procesar la transferencia") if isinstance(cuerpo, dict) else "Error al procesar la transferencia"
        return _error(r.status_code, detalle if isinstance(detalle, str) else "Error al procesar la transferencia", tiempo_ms=tiempo_ms)

    # No exponer UUID de cuentas ni la clave de idempotencia al navegador.
    transaccion = {
        k: v
        for k, v in cuerpo.items()
        if k not in ("cuenta_origen_id", "cuenta_destino_id", "id_idempotencia")
    }
    return {
        "transaccion": transaccion,
        "tiempo_ms": tiempo_ms,
        "otel_trace_id": otel_trace_id,
        "jaeger_trace_url": f"{URL_JAEGER}/trace/{otel_trace_id}",
    }


@app.get("/demo/analisis/{id_transaccion}")
async def analisis(id_transaccion: UUID):
    """Consulta el analisis ya persistido: GET servicio_ia /analisis/{id}/admin."""
    try:
        async with _cliente() as c:
            r = await c.get(f"{URL_IA}/analisis/{id_transaccion}/admin")
    except httpx.HTTPError:
        return JSONResponse(
            status_code=503, content={"detail": "Servicio IA no disponible"}
        )

    if r.status_code == 404:
        # La IA es asincrona: todavia no hay resultado. No es un error.
        return {"estado": "PENDIENTE", "id_transaccion": str(id_transaccion)}
    if r.status_code != 200:
        return JSONResponse(
            status_code=502, content={"detail": "Respuesta inesperada del servicio IA"}
        )

    a = r.json()
    return {
        "estado": "DISPONIBLE",
        "id_transaccion": a["id_transaccion"],
        "nivel": a["nivel"],
        "score": a["score"],
        "modelo": a["modelo"],
        "recomendacion": a["recomendacion"],
    }


# ---------------------------------------------------------------------- ETL

_etl_lock = threading.Lock()


# El ETL real (etl.transformar.transformar) lee CSV. Un .xlsx se convierte (primera hoja)
# a un CSV temporal en la frontera, para ejecutar exactamente el mismo pipeline.
EXTENSIONES_ETL = {".csv", ".xlsx"}
MAX_MB_ETL = int(os.getenv("DEMO_ETL_MAX_MB", "20"))
_TROZO = 1024 * 1024


def _excel_a_csv(origen: Path, destino: Path) -> None:
    """Primera hoja del .xlsx -> CSV. Todo como texto: el ETL normaliza montos y fechas."""
    df = pd.read_excel(origen, dtype=str, engine="openpyxl")
    if len(df.columns) == 0:
        raise pd_errors.EmptyDataError("hoja vacia")
    df.to_csv(destino, index=False)


def _fallo_etl(status: int, mensaje: str, archivo: str | None = None, inicio: float | None = None):
    contenido = {"estado": "FALLIDO", "detalle": mensaje}
    if archivo:
        contenido["archivo"] = archivo
    if inicio is not None:
        contenido["duracion_ms"] = round((time.perf_counter() - inicio) * 1000)
    return JSONResponse(status_code=status, content=contenido)


@app.post("/demo/etl/ejecutar")
def ejecutar_etl(archivo: UploadFile | None = File(default=None)):
    """Recibe un CSV o XLSX (multipart, campo `archivo`) y lo procesa con etl.transformar.transformar().

    Solo se usa el contenido: el nombre enviado por el usuario nunca forma parte de una ruta.
    """
    if archivo is None or not (archivo.filename or "").strip():
        return _fallo_etl(400, "Debe seleccionar un archivo.")
    nombre = Path(archivo.filename.replace("\\", "/")).name  # solo para mostrar
    if Path(nombre).suffix.lower() not in EXTENSIONES_ETL:
        return _fallo_etl(415, "Formato de archivo no soportado. Solo se admiten archivos .csv o .xlsx.", nombre)

    if not _etl_lock.acquire(blocking=False):
        return JSONResponse(status_code=409, content={"estado": "FALLIDO", "detalle": "El ETL ya está en ejecución."})
    inicio = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="smartbancs_etl_") as carpeta:
            es_excel = Path(nombre).suffix.lower() == ".xlsx"
            recibido = Path(carpeta) / ("entrada.xlsx" if es_excel else "entrada.csv")  # nombre fijo
            ruta = Path(carpeta) / "entrada.csv"
            maximo = MAX_MB_ETL * _TROZO
            tamano = 0
            with open(recibido, "wb") as destino:
                while trozo := archivo.file.read(_TROZO):
                    tamano += len(trozo)
                    if tamano > maximo:
                        return _fallo_etl(413, f"El archivo supera el tamaño máximo de {MAX_MB_ETL} MB.", nombre)
                    destino.write(trozo)
            if tamano == 0 or not recibido.read_bytes().strip():
                return _fallo_etl(400, "El archivo está vacío.", nombre)
            if es_excel:
                try:
                    _excel_a_csv(recibido, ruta)
                except pd_errors.EmptyDataError:
                    return _fallo_etl(400, "El archivo está vacío.", nombre)
                except Exception:
                    logger.exception("ETL: xlsx invalido")
                    return _fallo_etl(422, "El archivo Excel no es válido.", nombre)

            from etl.transformar import transformar

            try:
                resultado = transformar(archivo_entrada=ruta)
            except pd_errors.EmptyDataError:
                return _fallo_etl(400, "El archivo está vacío.", nombre)
            except ValueError as exc:
                if str(exc).startswith("Faltan columnas obligatorias"):
                    faltan = str(exc).split(": ", 1)[-1]
                    return _fallo_etl(422, f"El archivo no contiene las columnas requeridas: {faltan}.", nombre)
                logger.exception("ETL: error de formato")
                return _fallo_etl(422, "El archivo no tiene un formato CSV válido.", nombre)
            except (pd_errors.ParserError, UnicodeDecodeError):
                return _fallo_etl(422, "El archivo no tiene un formato CSV válido.", nombre)
            except OSError:
                logger.exception("ETL: error durante la carga")
                return _fallo_etl(500, "Error durante la carga.", nombre, inicio)
            except Exception:
                logger.exception("ETL: error durante la transformacion")
                return _fallo_etl(500, "Error durante la transformación.", nombre, inicio)

        limpios = len(resultado["limpios"])
        rechazados = len(resultado["observados"])
        return {
            "estado": "COMPLETADO",
            "archivo": nombre,
            "registros_leidos": limpios + rechazados,
            "registros_transformados": limpios,
            "registros_cargados": limpios,  # el ETL "carga" escribiendo el CSV limpio
            "registros_rechazados": rechazados,  # observados por calidad de datos
            "duracion_ms": round((time.perf_counter() - inicio) * 1000),
        }
    finally:
        _etl_lock.release()


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
