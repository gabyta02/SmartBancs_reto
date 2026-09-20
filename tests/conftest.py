import os


# Las pruebas verifican la propagacion de contexto sin enviar trazas a Jaeger.
os.environ["OTEL_TRACES_EXPORTER"] = "none"
