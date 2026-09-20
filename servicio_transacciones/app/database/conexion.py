import os
import threading
import time

from psycopg2 import InterfaceError, OperationalError, errors
from psycopg2.extensions import TRANSACTION_STATUS_UNKNOWN
from psycopg2.pool import PoolError, ThreadedConnectionPool

from servicio_transacciones.app.configuracion import settings
from servicio_transacciones.app.observabilidad.metricas import (
    DB_POOL_ADQUISICIONES_TOTAL,
    DB_POOL_ESPERA_SECONDS,
    DB_POOL_PRESTADAS,
    DB_POOL_GETCONN_SECONDS,
    DB_POOL_PUTCONN_SECONDS,
    DB_POOL_RECHAZOS_TOTAL,
    DB_POOL_SEMAFORO_WAIT_SECONDS,
    DB_POOL_TIMEOUTS_TOTAL,
    worker_actual,
)


class PoolAgotado(Exception):
    """Sin conexion disponible. `motivo`: cola_llena | timeout | pool_error."""

    def __init__(self, mensaje="", motivo="desconocido"):
        super().__init__(mensaje)
        self.motivo = motivo


# SQLSTATE con los que PostgreSQL informa que cerro la sesion (no que fallo una sentencia).
_SQLSTATE_SESION_CERRADA = {"25P03", "57P01", "57P02", "57P03"}

_pool = None
_pool_pid = None
_pool_lock = threading.Lock()


class PoolConEspera:
    """Limita prestamos y espera sin acumular una cola ilimitada por worker."""

    def __init__(self, pool, capacidad):
        self.pool = pool
        self.slots = threading.BoundedSemaphore(capacidad)
        self.cola = threading.BoundedSemaphore(min(10, capacidad))

    def _adquirir_slot(self):
        adquirido = self.slots.acquire(blocking=False)
        if not adquirido:
            if not self.cola.acquire(blocking=False):
                DB_POOL_RECHAZOS_TOTAL.labels(motivo="cola_llena").inc()
                raise PoolAgotado("Cola del pool llena", motivo="cola_llena")
            try:
                adquirido = self.slots.acquire(timeout=max(0, settings.db_pool_timeout_s))
            finally:
                self.cola.release()
            if not adquirido:
                DB_POOL_TIMEOUTS_TOTAL.inc()
                DB_POOL_RECHAZOS_TOTAL.labels(motivo="timeout").inc()
                raise PoolAgotado("Tiempo de espera del pool agotado", motivo="timeout")

    def getconn(self):
        inicio = time.perf_counter()
        worker = worker_actual()
        inicio_semaforo = time.perf_counter()
        try:
            self._adquirir_slot()
        finally:
            DB_POOL_SEMAFORO_WAIT_SECONDS.labels(worker=worker).observe(
                time.perf_counter() - inicio_semaforo
            )
        inicio_getconn = time.perf_counter()
        try:
            conn = self.pool.getconn()
        except PoolError as exc:
            self.slots.release()
            DB_POOL_RECHAZOS_TOTAL.labels(motivo="pool").inc()
            raise PoolAgotado("No hay conexiones disponibles", motivo="pool_error") from exc
        except BaseException:
            self.slots.release()
            raise
        finally:
            DB_POOL_GETCONN_SECONDS.labels(worker=worker).observe(
                time.perf_counter() - inicio_getconn
            )
        DB_POOL_ESPERA_SECONDS.observe(time.perf_counter() - inicio)
        DB_POOL_ADQUISICIONES_TOTAL.inc()
        DB_POOL_PRESTADAS.inc()
        return conn

    def putconn(self, conn, close=False):
        inicio_putconn = time.perf_counter()
        try:
            self.pool.putconn(conn, close=close)
        finally:
            DB_POOL_PUTCONN_SECONDS.labels(worker=worker_actual()).observe(
                time.perf_counter() - inicio_putconn
            )
            DB_POOL_PRESTADAS.dec()
            self.slots.release()


def _obtener_pool():
    global _pool, _pool_pid
    with _pool_lock:
        if _pool is None or _pool_pid != os.getpid():
            interno = ThreadedConnectionPool(
                minconn=settings.db_pool_min_conn,
                maxconn=settings.db_pool_size + settings.db_max_overflow,
                host=settings.db_host,
                port=settings.db_port,
                dbname=settings.db_name,
                user=settings.db_user,
                password=settings.db_password,
                application_name="smartbancs",
            )
            _pool = PoolConEspera(
                interno, settings.db_pool_size + settings.db_max_overflow,
            )
            _pool_pid = os.getpid()
        return _pool


def error_de_conexion_perdida(exc) -> bool:
    """True si `exc` indica que la conexion (no la sentencia) dejo de ser valida.

    QueryCanceled (57014), LockNotAvailable (55P03) y DeadlockDetected (40P01)
    tambien son OperationalError, pero la conexion sigue sana: se reutiliza.
    """
    if isinstance(exc, InterfaceError):
        return True
    if not isinstance(exc, OperationalError):
        return False
    if isinstance(exc, (errors.QueryCanceled, errors.LockNotAvailable,
                        errors.TransactionRollbackError)):
        return False
    pgcode = getattr(exc, "pgcode", None)
    return pgcode is None or pgcode in _SQLSTATE_SESION_CERRADA or pgcode.startswith("08")


def conexion_inutilizable(conn) -> bool:
    """True si la conexion esta cerrada o el servidor la perdio."""
    if conn.closed:
        return True
    try:
        return conn.info.transaction_status == TRANSACTION_STATUS_UNKNOWN
    except Exception:
        return True


def obtener_conexion():
    pool = _obtener_pool()
    conn = pool.getconn()
    invalida = False
    try:
        yield conn
    except (OperationalError, InterfaceError) as exc:
        invalida = error_de_conexion_perdida(exc)
        raise
    finally:
        # Nunca entregar al siguiente request una transaccion abierta o fallida,
        # ni una conexion que PostgreSQL ya cerro: esas se descartan (close=True).
        rota = invalida or conexion_inutilizable(conn)
        if not rota:
            try:
                conn.rollback()
            except Exception:
                rota = True
        pool.putconn(conn, close=rota)
