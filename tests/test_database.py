import hashlib
import os
import uuid

import psycopg2
import pytest
from dotenv import load_dotenv
from psycopg2 import errors


load_dotenv()


# ---------------------------------------------------------------------
# Configuración de base de datos
# ---------------------------------------------------------------------

DB_HOST = os.getenv("POSTGRES_HOST", "127.0.0.1")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB")

ADMIN_USER = os.getenv("POSTGRES_USER")
ADMIN_PASS = os.getenv("POSTGRES_PASSWORD")

APP_USER = os.getenv("DB_APP_USER")
APP_PASS = os.getenv("DB_APP_PASSWORD")


# ---------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------

def _connect(user, password):
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=user,
        password=password,
    )


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def admin_conn():
    """
    Conexión con usuario administrador.

    Se utiliza para verificar:
    - estructura
    - constraints
    - triggers
    - comportamiento interno de la base de datos

    Al finalizar cada prueba se ejecuta rollback.
    """

    conn = _connect(
        ADMIN_USER,
        ADMIN_PASS,
    )

    yield conn

    conn.rollback()
    conn.close()


@pytest.fixture
def app_conn():
    """
    Conexión con el usuario restringido del microservicio.

    Permite comprobar:
    - permisos DML
    - restricciones DDL
    """

    conn = _connect(
        APP_USER,
        APP_PASS,
    )

    yield conn

    conn.rollback()
    conn.close()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def crear_cuenta(cur, saldo=100):
    """
    Inserta una cuenta temporal para las pruebas.

    Retorna:
        UUID de la cuenta creada.
    """

    cur.execute(
        """
        INSERT INTO cuentas (
            numero_cuenta,
            nombre_titular,
            saldo
        )
        VALUES (%s, %s, %s)
        RETURNING id_cuenta
        """,
        (
            f"TEST-{uuid.uuid4().hex[:20]}",
            "Titular de prueba",
            saldo,
        ),
    )

    return cur.fetchone()[0]


def generar_hash_solicitud(
    origen,
    destino,
    monto,
    divisa="USD",
):
    """
    Genera un SHA-256 determinista para representar
    el contenido de una solicitud de transferencia.

    SHA-256 produce 64 caracteres hexadecimales,
    compatible con hash_solicitud CHAR(64).
    """

    contenido = (
        f"{origen}|"
        f"{destino}|"
        f"{monto}|"
        f"{divisa}"
    )

    return hashlib.sha256(
        contenido.encode("utf-8")
    ).hexdigest()


def insertar_transaccion(
    cur,
    origen,
    destino,
    monto,
    idempotencia=None,
    hash_solicitud=None,
):
    """
    Inserta una transacción de prueba.

    Retorna:
        UUID de la transacción creada.
    """

    clave_idempotencia = (
        idempotencia
        or f"idem-{uuid.uuid4().hex}"
    )

    hash_final = (
        hash_solicitud
        or generar_hash_solicitud(
            origen,
            destino,
            monto,
        )
    )

    cur.execute(
        """
        INSERT INTO transacciones (
            id_idempotencia,
            hash_solicitud,
            cuenta_origen_id,
            cuenta_destino_id,
            monto
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id_transaccion
        """,
        (
            clave_idempotencia,
            hash_final,
            origen,
            destino,
            monto,
        ),
    )

    return cur.fetchone()[0]


# =====================================================================
# TESTS DE ESTRUCTURA
# =====================================================================

def test_existencia_tablas_principales(admin_conn):
    """
    Comprueba que existan las tablas principales
    necesarias para el sistema transaccional.
    """

    with admin_conn.cursor() as cur:

        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            """
        )

        tablas = {
            fila[0]
            for fila in cur.fetchall()
        }

    assert {
        "cuentas",
        "transacciones",
        "movimientos",
        "eventos_outbox",
    } <= tablas


# =====================================================================
# TESTS DE CONSTRAINTS
# =====================================================================

def test_saldo_no_puede_ser_negativo(admin_conn):
    """
    Una cuenta nunca puede tener saldo negativo.
    """

    with admin_conn.cursor() as cur:

        with pytest.raises(
            errors.CheckViolation
        ):
            crear_cuenta(
                cur,
                saldo=-1,
            )


def test_monto_transaccion_debe_ser_positivo(admin_conn):
    """
    Una transferencia debe tener monto > 0.
    """

    with admin_conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        with pytest.raises(
            errors.CheckViolation
        ):
            insertar_transaccion(
                cur,
                origen,
                destino,
                monto=0,
            )


def test_transferencia_no_permite_misma_cuenta(admin_conn):
    """
    La cuenta origen y destino deben ser diferentes.
    """

    with admin_conn.cursor() as cur:

        cuenta = crear_cuenta(cur)

        with pytest.raises(
            errors.CheckViolation
        ):
            insertar_transaccion(
                cur,
                cuenta,
                cuenta,
                monto=10,
            )


# =====================================================================
# TESTS DE IDEMPOTENCIA
# =====================================================================

def test_idempotencia_es_unica(admin_conn):
    """
    Dos transacciones no pueden utilizar
    el mismo id de idempotencia.
    """

    with admin_conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        clave = (
            f"idem-{uuid.uuid4().hex}"
        )

        insertar_transaccion(
            cur,
            origen,
            destino,
            monto=10,
            idempotencia=clave,
        )

        with pytest.raises(
            errors.UniqueViolation
        ):
            insertar_transaccion(
                cur,
                origen,
                destino,
                monto=10,
                idempotencia=clave,
            )


def test_hash_solicitud_tiene_longitud_correcta():
    """
    SHA-256 debe generar exactamente
    64 caracteres hexadecimales.
    """

    hash_solicitud = generar_hash_solicitud(
        "origen",
        "destino",
        100,
    )

    assert len(hash_solicitud) == 64


# =====================================================================
# TESTS DE ESTADO DE TRANSACCIONES
# =====================================================================

def test_transaccion_puede_completarse(admin_conn):
    """
    Una transacción PENDIENTE puede cambiar
    correctamente a COMPLETADA.
    """

    with admin_conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        transaccion_id = insertar_transaccion(
            cur,
            origen,
            destino,
            monto=10,
        )

        cur.execute(
            """
            UPDATE transacciones
            SET
                estado = 'COMPLETADA',
                completado_en = CURRENT_TIMESTAMP
            WHERE id_transaccion = %s
            """,
            (transaccion_id,),
        )

        cur.execute(
            """
            SELECT
                estado,
                completado_en
            FROM transacciones
            WHERE id_transaccion = %s
            """,
            (transaccion_id,),
        )

        estado, completado_en = (
            cur.fetchone()
        )

        assert estado == "COMPLETADA"
        assert completado_en is not None


def test_transaccion_puede_rechazarse(admin_conn):
    """
    Una transacción PENDIENTE puede cambiar
    correctamente a RECHAZADA.
    """

    with admin_conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        transaccion_id = insertar_transaccion(
            cur,
            origen,
            destino,
            monto=10,
        )

        cur.execute(
            """
            UPDATE transacciones
            SET
                estado = 'RECHAZADA',
                razon_fallo = 'SALDO_INSUFICIENTE'
            WHERE id_transaccion = %s
            """,
            (transaccion_id,),
        )

        cur.execute(
            """
            SELECT
                estado,
                razon_fallo
            FROM transacciones
            WHERE id_transaccion = %s
            """,
            (transaccion_id,),
        )

        estado, razon_fallo = (
            cur.fetchone()
        )

        assert estado == "RECHAZADA"

        assert (
            razon_fallo
            == "SALDO_INSUFICIENTE"
        )


def test_transaccion_completada_requiere_fecha(admin_conn):
    """
    Una transacción no puede pasar a COMPLETADA
    si completado_en sigue siendo NULL.
    """

    with admin_conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        transaccion_id = insertar_transaccion(
            cur,
            origen,
            destino,
            monto=10,
        )

        with pytest.raises(
            errors.CheckViolation
        ):
            cur.execute(
                """
                UPDATE transacciones
                SET estado = 'COMPLETADA'
                WHERE id_transaccion = %s
                """,
                (transaccion_id,),
            )


def test_transaccion_completada_no_puede_volver_a_pendiente(
    admin_conn,
):
    """
    COMPLETADA es un estado final.

    No debe poder regresar a PENDIENTE.
    """

    with admin_conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        transaccion_id = insertar_transaccion(
            cur,
            origen,
            destino,
            monto=10,
        )

        cur.execute(
            """
            UPDATE transacciones
            SET
                estado = 'COMPLETADA',
                completado_en = CURRENT_TIMESTAMP
            WHERE id_transaccion = %s
            """,
            (transaccion_id,),
        )

        with pytest.raises(
            errors.CheckViolation
        ):
            cur.execute(
                """
                UPDATE transacciones
                SET estado = 'PENDIENTE'
                WHERE id_transaccion = %s
                """,
                (transaccion_id,),
            )


def test_transaccion_rechazada_no_puede_volver_a_pendiente(
    admin_conn,
):
    """
    RECHAZADA también es un estado final.
    """

    with admin_conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        transaccion_id = insertar_transaccion(
            cur,
            origen,
            destino,
            monto=10,
        )

        cur.execute(
            """
            UPDATE transacciones
            SET
                estado = 'RECHAZADA',
                razon_fallo = 'SALDO_INSUFICIENTE'
            WHERE id_transaccion = %s
            """,
            (transaccion_id,),
        )

        with pytest.raises(
            errors.CheckViolation
        ):
            cur.execute(
                """
                UPDATE transacciones
                SET estado = 'PENDIENTE'
                WHERE id_transaccion = %s
                """,
                (transaccion_id,),
            )


# =====================================================================
# TESTS DE INMUTABILIDAD
# =====================================================================

def test_transaccion_no_permite_modificar_monto(admin_conn):
    """
    El monto original de una transferencia
    debe permanecer inmutable.
    """

    with admin_conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        transaccion_id = insertar_transaccion(
            cur,
            origen,
            destino,
            monto=10,
        )

        with pytest.raises(
            errors.InsufficientPrivilege
        ):
            cur.execute(
                """
                UPDATE transacciones
                SET monto = 100
                WHERE id_transaccion = %s
                """,
                (transaccion_id,),
            )


def test_transaccion_no_permite_modificar_cuenta_origen(
    admin_conn,
):
    """
    La cuenta origen original debe ser inmutable.
    """

    with admin_conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        nueva_cuenta = crear_cuenta(cur)

        transaccion_id = insertar_transaccion(
            cur,
            origen,
            destino,
            monto=10,
        )

        with pytest.raises(
            errors.InsufficientPrivilege
        ):
            cur.execute(
                """
                UPDATE transacciones
                SET cuenta_origen_id = %s
                WHERE id_transaccion = %s
                """,
                (
                    nueva_cuenta,
                    transaccion_id,
                ),
            )


def test_transaccion_no_permite_modificar_idempotencia(
    admin_conn,
):
    """
    Una vez creada la transacción,
    su clave de idempotencia debe ser inmutable.
    """

    with admin_conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        transaccion_id = insertar_transaccion(
            cur,
            origen,
            destino,
            monto=10,
        )

        nueva_clave = (
            f"idem-{uuid.uuid4().hex}"
        )

        with pytest.raises(
            errors.InsufficientPrivilege
        ):
            cur.execute(
                """
                UPDATE transacciones
                SET id_idempotencia = %s
                WHERE id_transaccion = %s
                """,
                (
                    nueva_clave,
                    transaccion_id,
                ),
            )


def test_transaccion_no_puede_eliminarse(admin_conn):
    """
    Las transacciones financieras no pueden eliminarse.
    """

    with admin_conn.cursor() as cur:

        origen = crear_cuenta(cur)
        destino = crear_cuenta(cur)

        transaccion_id = insertar_transaccion(
            cur,
            origen,
            destino,
            monto=10,
        )

        with pytest.raises(
            errors.InsufficientPrivilege
        ):
            cur.execute(
                """
                DELETE FROM transacciones
                WHERE id_transaccion = %s
                """,
                (transaccion_id,),
            )


# =====================================================================
# TESTS DEL USUARIO DEL MICROSERVICIO
# =====================================================================

def test_usuario_servicio_puede_hacer_dml(app_conn):
    """
    El usuario del microservicio puede realizar
    las operaciones DML necesarias.
    """

    with app_conn.cursor() as cur:

        cuenta_id = crear_cuenta(
            cur,
            saldo=100,
        )

        cur.execute(
            """
            SELECT saldo
            FROM cuentas
            WHERE id_cuenta = %s
            """,
            (cuenta_id,),
        )

        saldo = cur.fetchone()[0]

        assert saldo == 100

        cur.execute(
            """
            UPDATE cuentas
            SET saldo = 50
            WHERE id_cuenta = %s
            """,
            (cuenta_id,),
        )

        assert cur.rowcount == 1

        cur.execute(
            """
            SELECT saldo
            FROM cuentas
            WHERE id_cuenta = %s
            """,
            (cuenta_id,),
        )

        saldo = cur.fetchone()[0]

        assert saldo == 50


def test_usuario_servicio_no_puede_hacer_ddl(app_conn):
    """
    El usuario del microservicio no debe
    tener permisos para modificar el esquema.
    """

    with app_conn.cursor() as cur:

        with pytest.raises(
            errors.InsufficientPrivilege
        ):
            cur.execute(
                """
                CREATE TABLE tabla_prohibida (
                    id INTEGER
                )
                """
            )