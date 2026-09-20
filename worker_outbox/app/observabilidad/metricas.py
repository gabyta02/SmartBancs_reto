from prometheus_client import Counter, Histogram

OUTBOX_EVENTOS_TOTAL = Counter(
    "smartbancs_outbox_eventos_total", "Resultados del procesamiento outbox", ["resultado"]
)
RABBITMQ_PUBLICACIONES_TOTAL = Counter(
    "smartbancs_rabbitmq_publicaciones_total", "Publicaciones en RabbitMQ", ["resultado"]
)
RABBITMQ_PUBLICACION_DURACION_SECONDS = Histogram(
    "smartbancs_rabbitmq_publicacion_duracion_seconds", "Duracion de basic_publish"
)
