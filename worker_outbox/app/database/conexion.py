import psycopg2
import pika

from worker_outbox.app.configuracion import settings


def crear_conexion():
    return psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        application_name="worker_outbox",
    )

def crear_conexion_rabbit():
    credenciales = pika.PlainCredentials(
        settings.rabbitmq_user,
        settings.rabbitmq_password,
    )
    parametros = pika.ConnectionParameters(
        host=settings.rabbitmq_host,
        port=settings.rabbitmq_port,
        credentials=credenciales,
    )
    return pika.BlockingConnection(parametros)
