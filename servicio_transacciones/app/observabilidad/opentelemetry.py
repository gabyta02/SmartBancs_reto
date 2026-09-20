from opentelemetry import trace
import os

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)

from opentelemetry.sdk.resources import (
    SERVICE_NAME,
    Resource,
)

from opentelemetry.sdk.trace import (
    TracerProvider,
)

from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
)

from opentelemetry.instrumentation.fastapi import (
    FastAPIInstrumentor,
)


def configurar_opentelemetry(
    app,
) -> None:

    resource = Resource.create(
        {
            SERVICE_NAME:
                "servicio_transacciones"
        }
    )

    provider = TracerProvider(
        resource=resource
    )

    if os.getenv("OTEL_TRACES_EXPORTER", "otlp").lower() != "none":
        exporter = OTLPSpanExporter(
            endpoint="http://jaeger:4317",
            insecure=True,
        )

        processor = BatchSpanProcessor(
            exporter
        )

        provider.add_span_processor(
            processor
        )

    trace.set_tracer_provider(
        provider
    )

    FastAPIInstrumentor.instrument_app(
        app
    )
