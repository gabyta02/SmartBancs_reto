import os

import psycopg2


def crear_conexion():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        application_name="consumidor_ia",
    )


def obtener_conexion():
    conn = crear_conexion()

    try:
        yield conn
    finally:
        conn.close()
