from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


_configurado = False


def configurar_opentelemetry() -> None:
    global _configurado
    if _configurado:
        return

    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: "worker_outbox"}),
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint="http://jaeger:4317", insecure=True)
        )
    )
    trace.set_tracer_provider(provider)
    _configurado = True
