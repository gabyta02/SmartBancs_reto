import asyncio
import hashlib
import json
import logging
import random
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from decimal import Decimal
from psycopg2 import Error as PsycopgError, InterfaceError, OperationalError, errors
from opentelemetry import trace
from opentelemetry.propagate import inject


from servicio_transacciones.app.repositorios.transacciones import (
    acreditar_cuenta,
    completar_transaccion,
    debitar_cuenta,
    insertar_evento_outbox,
    insertar_movimiento,
    obtener_cuentas_para_actualizar,
    rechazar_transaccion,
)

from servicio_transacciones.app.servicios.idempotencia import (
    obtener_o_crear_transaccion,
)

from servicio_transacciones.app.observabilidad.trazabilidad import (
    obtener_trace_id,
)

from servicio_transacciones.app.observabilidad.metricas import (
    DURACION_TRANSFERENCIA,
    DB_DEADLOCKS_TOTAL,
    DB_LOCK_WAIT_SECONDS,
    DB_OPERACIONES_TOTAL,
    DB_OPERACION_DURACION_SECONDS,
    DB_TIMEOUTS_TOTAL,
    DB_REINTENTOS_TOTAL,
    DB_SQL_DURACION_SECONDS,
    DB_TRANSACCION_DURACION_SECONDS,
    ERRORES_TRANSACCIONES_TOTAL,
    TRANSACCIONES_REPRODUCIDAS_TOTAL,
    TRANSACCIONES_TOTAL,
    registrar_error_http,
    worker_actual,
)

from servicio_transacciones.app.configuracion import settings
from servicio_transacciones.app.database.conexion import obtener_conexion

from servicio_transacciones.app.esquemas.transacciones import TransferenciaCreate, TransferenciaResponse
from servicio_transacciones.app.repositorios import transacciones as repo

logger = logging.getLogger("smartbancs.transferencias")
tracer = trace.get_tracer(
    "smartbancs.transferencias"
)




class CuentaNoEncontrada(Exception):
    pass


class CuentaNoDisponible(Exception):
    pass


# SQLSTATE que indican conflicto transitorio de concurrencia: se puede reintentar
# la transaccion COMPLETA con seguridad (fue revertida entera por PostgreSQL).
SQLSTATE_REINTENTABLES = {
    "40P01": "deadlock_detected",
    "40001": "serialization_failure",
    "55P03": "lock_not_available",  # disparado por lock_timeout
}


class CuentaNoEncontrada(Exception):
    pass


class ConflictoIdempotencia(Exception):
    """La clave ya se uso con un payload distinto."""


class ServicioSaturado(Exception):
    """Se agotaron reintentos o presupuesto de tiempo; el cliente puede reintentar con la misma clave."""

class _Duplicada(Exception):
    pass


@dataclass
class Resultado:
    respuesta: TransferenciaResponse
    reproducida: bool


def hash_solicitud(req: TransferenciaCreate) -> str:
    canonico = json.dumps({
        "o": req.cuenta_origen_id, "d": req.cuenta_destino_id,
        "m": str(req.monto.quantize(Decimal("0.01"))), "v": req.divisa,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonico.encode()).hexdigest()


def _a_respuesta(fila: dict, reproducida: bool) -> TransferenciaResponse:
    return TransferenciaResponse(
        id_transaccion=fila["id_transaccion"], id_idempotencia=fila["id_idempotencia"],
        cuenta_origen_id=fila["cuenta_origen_id"], cuenta_destino_id=fila["cuenta_destino_id"],
        monto=fila["monto"], divisa=fila["divisa"], estado=fila["estado"],
        razon_fallo=fila["razon_fallo"], creado_en=fila["creado_en"], reproducida=reproducida,
    )


def _validar_negocio(req: TransferenciaCreate, origen: dict, destino: dict) -> str | None:
    if origen["estado"] != "ACTIVA":
        return f"Cuenta origen {origen['estado'].lower()}"
    if destino["estado"] != "ACTIVA":
        return f"Cuenta destino {destino['estado'].lower()}"
    if origen["divisa"] != req.divisa or destino["divisa"] != req.divisa:
        return "Divisa no coincide con las cuentas"
    if origen["saldo"] < req.monto:
        return "Fondos insuficientes"
    return None


async def _reproducir(req: TransferenciaCreate, h: str) -> Resultado | None:
    async with SessionLocal() as s:
        previa = await repo.buscar_por_idempotencia(s, req.id_idempotencia)
    if previa is None:
        return None
    if previa["hash_solicitud"] != h:
        raise ConflictoIdempotencia(req.id_idempotencia)
    return Resultado(_a_respuesta(previa, reproducida=True), reproducida=True)


async def _intento(req: TransferenciaCreate, h: str, metricas: dict) -> Resultado:
    async with SessionLocal() as s:
        async with s.begin():  # BEGIN ... COMMIT (o ROLLBACK si hay excepcion)
            t0 = time.perf_counter()
            cuentas = await repo.bloquear_cuentas(s, req.cuenta_origen_id, req.cuenta_destino_id)
            # tiempo esperando locks: la metrica clave para diagnosticar contencion
            metricas["espera_bloqueo_ms"] = round((time.perf_counter() - t0) * 1000, 2)

            origen = cuentas.get(req.cuenta_origen_id)
            destino = cuentas.get(req.cuenta_destino_id)
            if origen is None or destino is None:
                raise CuentaNoEncontrada(req.cuenta_origen_id if origen is None else req.cuenta_destino_id)

            razon = _validar_negocio(req, origen, destino)
            estado = "RECHAZADA" if razon else "COMPLETADA"

            tx = await repo.insertar_transaccion(
                s, id_idempotencia=req.id_idempotencia, hash_solicitud=h,
                origen_id=origen["id_cuenta"], destino_id=destino["id_cuenta"],
                monto=req.monto, divisa=req.divisa, estado=estado, razon_fallo=razon,
            )
            if tx is None:
                # Otra solicitud con la misma clave confirmo primero. No tocamos saldos;
                # el context manager hace ROLLBACK al salir por la excepcion.
                raise _Duplicada()

            carga = {
                "id_transaccion": tx["id_transaccion"], "id_idempotencia": req.id_idempotencia,
                "cuenta_origen_id": req.cuenta_origen_id, "cuenta_destino_id": req.cuenta_destino_id,
                "monto": req.monto, "divisa": req.divisa, "razon_fallo": razon,
            }
            if razon is None:
                await repo.aplicar_transferencia(
                    s, tx["id_transaccion"], origen["id_cuenta"], destino["id_cuenta"], req.monto, carga)
            else:
                await repo.registrar_evento_rechazo(s, tx["id_transaccion"], carga)

        # COMMIT ya ocurrio aqui: los locks fueron liberados.
        return Resultado(_a_respuesta({
            **tx, "id_idempotencia": req.id_idempotencia, "cuenta_origen_id": req.cuenta_origen_id,
            "cuenta_destino_id": req.cuenta_destino_id, "monto": req.monto, "divisa": req.divisa,
            "estado": estado, "razon_fallo": razon,
        }, reproducida=False), reproducida=False)


async def transferir(req: TransferenciaCreate) -> Resultado:
    inicio = time.perf_counter()
    h = hash_solicitud(req)
    metricas: dict = {"id_idempotencia": req.id_idempotencia, "intentos": 0}

    # Sin pre-consulta de idempotencia: costaria una conexion y un round-trip extra
    # en CADA peticion. El INSERT ... ON CONFLICT dentro de la transaccion ya es el
    # arbitro atomico; solo los duplicados (raros) pagan la consulta adicional.
    for intento in range(1, settings.max_reintentos + 1):
        metricas["intentos"] = intento
        try:
            res = await _intento(req, h, metricas)
            _log("transferencia_procesada", inicio, metricas, estado=res.respuesta.estado)
            return res
        except PoolAgotado as e:
            # Todas las conexiones ocupadas: fallar rapido (503) es mejor que encolar
            # peticiones que igual superarian los 2 s y amplificarian el pico.
            logger.warning(json.dumps({"evento": "pool_agotado", **metricas}))
            raise ServicioSaturado("pool_agotado") from e
        except _Duplicada:
            try:
                previo = await _reproducir(req, h)
            except PoolAgotado as e:
                raise ServicioSaturado("pool_agotado") from e
            _log("transferencia_reproducida", inicio, metricas, estado=previo.respuesta.estado)
            return previo
        except DBAPIError as e:
            sqlstate = getattr(e.orig, "sqlstate", None)
            motivo = SQLSTATE_REINTENTABLES.get(sqlstate)
            transcurrido = time.perf_counter() - inicio
            espera = min(0.05 * 2 ** (intento - 1), 0.4) * random.uniform(0.5, 1.0)  # backoff + jitter
            puede = (motivo and intento < settings.max_reintentos
                     and transcurrido + espera < settings.presupuesto_transferencia_s)
            logger.warning(json.dumps({
                "evento": "conflicto_concurrencia" if motivo else "error_bd",
                "sqlstate": sqlstate, "motivo": motivo or type(e.orig).__name__,
                "reintenta": bool(puede), **metricas,
            }))
            if not puede:
                if motivo or sqlstate == "57014":  # 57014 = statement_timeout
                    raise ServicioSaturado(sqlstate) from e
                raise
            await asyncio.sleep(espera)
    raise ServicioSaturado("reintentos_agotados")


def _log(evento: str, inicio: float, metricas: dict, **extra) -> None:
    logger.info(json.dumps({
        "evento": evento, "duracion_ms": round((time.perf_counter() - inicio) * 1000, 2),
        **metricas, **extra,
    }))


def obtener_sqlstate(exc):
    """Extrae SQLSTATE de psycopg2 y de excepciones que lo envuelven."""
    origen = getattr(exc, "orig", exc)
    return getattr(origen, "pgcode", None) or getattr(origen, "sqlstate", None)


def _registrar_error_bd(exc, trace_id):
    sqlstate = obtener_sqlstate(exc)
    if sqlstate == "40P01":
        tipo, evento = "deadlock", "db_deadlock"
        DB_DEADLOCKS_TOTAL.inc()
    elif sqlstate in {"57014", "55P03"}:
        tipo, evento = "timeout", "db_timeout"
        DB_TIMEOUTS_TOTAL.inc()
    else:
        tipo, evento = "db_error", "db_error"
    ERRORES_TRANSACCIONES_TOTAL.labels(tipo=tipo).inc()
    logger.error("Error de PostgreSQL", extra={
        "evento": evento, "trace_id": trace_id, "sqlstate": sqlstate,
        "operacion": getattr(exc, "operacion_db", "transferencia"),
        "tipo_error": type(exc).__name__,
    })


def procesar_transferencia(*args, **kwargs):
    # Los llamadores directos de las pruebas pueden aportar su conexion.
    # La ruta HTTP no la aporta: cada intento obtiene y libera su propio lease.
    conn_externa = kwargs.pop("conn", None)
    if args and hasattr(args[0], "cursor"):
        conn_externa, args = args[0], args[1:]
    inicio_transferencia = time.perf_counter()
    for intento in range(1, max(1, min(settings.max_reintentos, 3)) + 1):
        inicio_intento = time.perf_counter()
        demora = None
        try:
            prestamo = (nullcontext(conn_externa) if conn_externa is not None
                        else contextmanager(obtener_conexion)())
            with prestamo as conn:
                resultado = _procesar_transferencia(conn, *args, **kwargs)
            _registrar_resultado(resultado, inicio_transferencia)
            return resultado
        except PsycopgError as exc:
            sqlstate = obtener_sqlstate(exc)
            _registrar_error_bd(exc, obtener_trace_id())
            if sqlstate not in {"57014", "55P03", "40P01"}:
                registrar_error_http(500, "db_error", sqlstate or "desconocido")
                exc.error_http_registrado = True
                raise
            if intento >= max(1, min(settings.max_reintentos, 3)):
                raise ServicioSaturado(sqlstate) from exc
            DB_REINTENTOS_TOTAL.labels(sqlstate=sqlstate).inc()
            demora = 0.025 * 2 ** (intento - 1) * random.uniform(0.5, 1.5)
        finally:
            DB_TRANSACCION_DURACION_SECONDS.observe(time.perf_counter() - inicio_intento)
        if demora is not None:
            time.sleep(demora)


def _registrar_resultado(resultado, inicio):
    if resultado["reproducida"]:
        TRANSACCIONES_REPRODUCIDAS_TOTAL.inc()
    TRANSACCIONES_TOTAL.labels(estado=resultado["estado"]).inc()
    DURACION_TRANSFERENCIA.observe(time.perf_counter() - inicio)
    logger.info(
        "Transferencia procesada",
        extra={
            "evento": "transferencia_reproducida" if resultado["reproducida"] else
                      "transferencia_completada" if resultado["estado"] == "COMPLETADA" else
                      "transferencia_rechazada",
            "trace_id": obtener_trace_id(),
            "id_transaccion": str(resultado["id_transaccion"]),
            "estado": resultado["estado"],
            "motivo": resultado["razon_fallo"],
        },
    )


def _clasificar_rechazo(cuentas, origen_id, destino_id, divisa, monto):
    if len(cuentas) != 2 or origen_id not in cuentas or destino_id not in cuentas:
        raise CuentaNoEncontrada("Una de las cuentas no existe.")
    origen, destino = cuentas[origen_id], cuentas[destino_id]
    if origen["estado"] != "ACTIVA":
        return "CUENTA_ORIGEN_NO_ACTIVA"
    if destino["estado"] != "ACTIVA":
        return "CUENTA_DESTINO_NO_ACTIVA"
    if origen["divisa"] != divisa or destino["divisa"] != divisa:
        return "DIVISA_NO_COMPATIBLE"
    if origen["saldo"] < monto:
        return "SALDO_INSUFICIENTE"
    return None


@contextmanager
def _medir_fase_sql(operacion):
    inicio = time.perf_counter()
    try:
        yield
    finally:
        DB_SQL_DURACION_SECONDS.labels(
            worker=worker_actual(), operacion=operacion,
        ).observe(time.perf_counter() - inicio)


class _MedirFinTransaccion:
    """Delega el context manager de psycopg2 y cronometra solo su salida."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn.__enter__()

    def __exit__(self, exc_type, exc, tb):
        operacion = "commit" if exc_type is None else "rollback"
        with _medir_fase_sql(operacion):
            if exc_type is None:
                # Un commit imposible (conexion cerrada) debe fallar, no ocultarse.
                return self.conn.__exit__(exc_type, exc, tb)
            # Ruta de error: la excepcion original manda. Con la conexion ya cerrada
            # no hay nada que revertir (el servidor aborto la transaccion) y un
            # rollback fallido no debe taparla.
            if self.conn.closed:
                return False
            try:
                return self.conn.__exit__(exc_type, exc, tb)
            except (OperationalError, InterfaceError):
                return False


def _procesar_transferencia(
    conn, id_idempotencia, hash_solicitud, cuenta_origen_id,
    cuenta_destino_id, monto, divisa="USD",
):
    trace_id = obtener_trace_id()
    cuenta_origen_id = str(cuenta_origen_id)
    cuenta_destino_id = str(cuenta_destino_id)
    monto = Decimal(str(monto)).quantize(Decimal("0.01"))
    divisa = divisa.upper()
    estado = None
    razon = None
    reproducida = False
    contexto_traza = {}
    inject(contexto_traza)

    try:
        with _MedirFinTransaccion(conn):
            try:
                with tracer.start_as_current_span("idempotencia.obtener_o_crear"):
                    idem = obtener_o_crear_transaccion(
                        conn=conn, id_idempotencia=id_idempotencia,
                        hash_solicitud=hash_solicitud,
                        cuenta_origen_id=cuenta_origen_id,
                        cuenta_destino_id=cuenta_destino_id,
                        monto=monto, divisa=divisa,
                    )
            except errors.ForeignKeyViolation as exc:
                raise CuentaNoEncontrada("Una de las cuentas no existe.") from exc

            reproducida = not idem["creada"]
            if not reproducida:
                transaccion_id = idem["transaccion"]["id_transaccion"]
                carga_util = json.dumps({
                    "trace_id": trace_id,
                    "trace_context": contexto_traza,
                    "id_transaccion": str(transaccion_id),
                    "cuenta_origen_id": cuenta_origen_id,
                    "cuenta_destino_id": cuenta_destino_id,
                    "monto": str(monto),
                    "divisa": divisa,
                    "estado": "COMPLETADA",
                })
                with conn.cursor() as cur:
                    # Los UUID canonicos tienen el mismo orden lexicografico
                    # que sus bytes. Todas las transferencias toman las filas
                    # en ese orden, incluso cuando destino se acredita primero.
                    cuentas_ordenadas = sorted((cuenta_origen_id, cuenta_destino_id))

                    def actualizar_saldos():
                        saldos = {}
                        with tracer.start_as_current_span("db.actualizar_saldos") as span:
                            span.set_attribute("db.system.name", "postgresql")
                            span.set_attribute("db.operation.name", "UPDATE")
                            inicio_saldos = time.perf_counter()
                            try:
                                for cuenta_id in cuentas_ordenadas:
                                    try:
                                        if cuenta_id == cuenta_origen_id:
                                            saldo = debitar_cuenta(cur, cuenta_id, monto, divisa)
                                        else:
                                            saldo = acreditar_cuenta(cur, cuenta_id, monto, divisa)
                                    except PsycopgError as exc:
                                        exc.operacion_db = "actualizar_saldos"
                                        raise
                                    if saldo is None:
                                        return None
                                    saldos[cuenta_id] = saldo
                            finally:
                                span.set_attribute(
                                    "db.duration_ms",
                                    round((time.perf_counter() - inicio_saldos) * 1000, 2),
                                )
                        return saldos

                    cur.execute("SAVEPOINT sp_transferencia_saldos")
                    saldos = actualizar_saldos()
                    if saldos is None:
                        # Deshace tambien el credito cuando destino tiene el
                        # UUID menor. La transaccion idempotente sigue viva.
                        cur.execute("ROLLBACK TO SAVEPOINT sp_transferencia_saldos")
                        cur.execute("RELEASE SAVEPOINT sp_transferencia_saldos")
                        with tracer.start_as_current_span("db.clasificar_rechazo"):
                            with tracer.start_as_current_span("db.bloquear_cuentas") as span_bloqueo:
                                span_bloqueo.set_attribute("db.system.name", "postgresql")
                                span_bloqueo.set_attribute("db.operation.name", "SELECT_FOR_NO_KEY_UPDATE")
                                inicio_bloqueo = time.perf_counter()
                                try:
                                    cuentas = obtener_cuentas_para_actualizar(
                                        cur, cuenta_origen_id, cuenta_destino_id,
                                    )
                                    span_bloqueo.set_attribute("smartbancs.accounts_locked", len(cuentas))
                                    DB_OPERACIONES_TOTAL.labels(
                                        operacion="bloquear_cuentas", resultado="ok"
                                    ).inc()
                                except PsycopgError as exc:
                                    exc.operacion_db = "bloquear_cuentas"
                                    DB_OPERACIONES_TOTAL.labels(
                                        operacion="bloquear_cuentas", resultado="error"
                                    ).inc()
                                    raise
                                finally:
                                    duracion = time.perf_counter() - inicio_bloqueo
                                    span_bloqueo.set_attribute("db.lock_wait_ms", round(duracion * 1000, 2))
                                    DB_LOCK_WAIT_SECONDS.observe(duracion)
                                    DB_OPERACION_DURACION_SECONDS.labels(
                                        operacion="bloquear_cuentas"
                                    ).observe(duracion)
                        razon = _clasificar_rechazo(
                            cuentas, cuenta_origen_id, cuenta_destino_id, divisa, monto,
                        )
                        if razon is None:
                            # El estado cambio durante la clasificacion. Con
                            # ambas filas bloqueadas ya puede repetirse seguro.
                            cur.execute("SAVEPOINT sp_transferencia_saldos")
                            saldos = actualizar_saldos()
                            if saldos is None:
                                raise RuntimeError("Las cuentas cambiaron estando bloqueadas")
                            cur.execute("RELEASE SAVEPOINT sp_transferencia_saldos")
                    else:
                        cur.execute("RELEASE SAVEPOINT sp_transferencia_saldos")

                    if razon:
                        rechazar_transaccion(cur, transaccion_id, razon)
                        estado = "RECHAZADA"
                    else:
                        with tracer.start_as_current_span("db.registrar_movimientos") as span:
                            span.set_attribute("db.system.name", "postgresql")
                            span.set_attribute("db.operation.name", "INSERT")
                            span.set_attribute("smartbancs.movements", 2)
                            insertar_movimiento(
                                cur, transaccion_id, cuenta_origen_id, "DEBITO",
                                monto, saldos[cuenta_origen_id],
                            )
                            insertar_movimiento(
                                cur, transaccion_id, cuenta_destino_id, "CREDITO",
                                monto, saldos[cuenta_destino_id],
                            )
                        with tracer.start_as_current_span("db.completar_transaccion") as span:
                            span.set_attribute("db.system.name", "postgresql")
                            span.set_attribute("db.operation.name", "UPDATE")
                            completar_transaccion(cur, transaccion_id)
                        with tracer.start_as_current_span("outbox.insertar_evento") as span:
                            span.set_attribute("db.system.name", "postgresql")
                            span.set_attribute("db.operation.name", "INSERT")
                            span.set_attribute("messaging.system", "rabbitmq")
                            span.set_attribute("messaging.destination.name", "cola_ia")
                            span.set_attribute("messaging.operation.name", "create")
                            span.set_attribute("smartbancs.outbox.event_type", "TRANSFERENCIA_COMPLETADA")
                            insertar_evento_outbox(
                                cur, transaccion_id, "TRANSFERENCIA_COMPLETADA", carga_util,
                            )
                        estado = "COMPLETADA"
    except Exception:
        # with conn revierte; este rollback tambien protege conexiones simuladas
        # y deja la conexion reutilizable antes de un reintento.
        if not conn.closed:  # cerrada: el servidor ya aborto la transaccion
            try:
                conn.rollback()
            except PsycopgError:
                logger.exception("No se pudo revertir la conexion", extra={"trace_id": trace_id})
        raise

    # El commit ya termino y libero los locks. No se ejecuta mas SQL: la respuesta
    # se arma con lo leido/devuelto (RETURNING) dentro de la transaccion, de modo
    # que la conexion vuelve al pool sin abrir otra transaccion.
    if reproducida:
        fila = idem["transaccion"]
    else:
        fila = {
            "id_transaccion": transaccion_id,
            "id_idempotencia": id_idempotencia,
            "cuenta_origen_id": cuenta_origen_id,
            "cuenta_destino_id": cuenta_destino_id,
            "monto": monto,
            "divisa": divisa,
            "estado": estado,
            "razon_fallo": razon,
            "creado_en": idem["transaccion"]["creado_en"],
        }
    resultado = {
        "id_transaccion": fila["id_transaccion"],
        "id_idempotencia": fila["id_idempotencia"],
        "cuenta_origen_id": fila["cuenta_origen_id"],
        "cuenta_destino_id": fila["cuenta_destino_id"],
        "monto": fila["monto"],
        "divisa": fila["divisa"],
        "estado": fila["estado"],
        "razon_fallo": fila["razon_fallo"],
        "creado_en": fila["creado_en"],
        "reproducida": reproducida,
    }
    return resultado
