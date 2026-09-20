from contextvars import ContextVar
from uuid import uuid4


trace_id_context: ContextVar[
    str | None
] = ContextVar(
    "trace_id",
    default=None,
)


def generar_trace_id() -> str:
    return str(
        uuid4()
    )


def establecer_trace_id(
    trace_id: str,
) -> None:
    trace_id_context.set(
        trace_id
    )


def obtener_trace_id() -> str | None:
    return trace_id_context.get()