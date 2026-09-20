import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from opentelemetry.propagate import inject
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from servicio_ia.app import consumidor
from worker_outbox.app import publicador


def _evento(trace_context=None):
    carga = {"monto": "30.00", "divisa": "USD"}
    if trace_context is not None:
        carga["trace_context"] = trace_context
    return {
        "id_eventos": "11111111-1111-1111-1111-111111111111",
        "transaccion_id": "22222222-2222-2222-2222-222222222222",
        "tipo_evento": "TRANSFERENCIA_COMPLETADA",
        "carga_util": carga,
    }


def _tracer_de_prueba(monkeypatch):
    exportador = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exportador))
    monkeypatch.setattr(publicador, "tracer", provider.get_tracer("test.worker"))
    monkeypatch.setattr(consumidor, "tracer", provider.get_tracer("test.consumer"))
    return provider.get_tracer("test.http"), exportador


def test_contexto_viaja_del_outbox_al_consumidor(monkeypatch):
    tracer_http, exportador = _tracer_de_prueba(monkeypatch)
    rabbit = MagicMock()
    rabbit.is_open = True
    monkeypatch.setattr(publicador, "crear_conexion_rabbit", lambda: rabbit)
    db = MagicMock()
    guardar = MagicMock()
    monkeypatch.setattr(consumidor, "crear_conexion", lambda: db)
    monkeypatch.setattr(consumidor, "guardar_analisis", guardar)

    with tracer_http.start_as_current_span("POST /transacciones"):
        with tracer_http.start_as_current_span("outbox.insertar_evento"):
            contexto_outbox = {}
            inject(contexto_outbox)

    publicador.publicar_evento_ia(_evento(contexto_outbox))
    mensaje = json.loads(rabbit.channel.return_value.basic_publish.call_args.kwargs["body"])
    assert mensaje["trace_context"]["traceparent"] != contexto_outbox["traceparent"]
    assert "trace_context" not in mensaje["datos"]

    canal = MagicMock()
    consumidor.callback(
        canal,
        SimpleNamespace(delivery_tag=7),
        None,
        json.dumps(mensaje).encode(),
    )
    guardar.assert_called_once()
    canal.basic_ack.assert_called_once_with(delivery_tag=7)
    canal.basic_nack.assert_not_called()

    spans = {span.name: span for span in exportador.get_finished_spans()}
    nombres = (
        "POST /transacciones", "outbox.insertar_evento",
        "worker_outbox.publicar_rabbitmq", "consumidor_ia.procesar",
        "ia.analizar_transaccion", "db.guardar_analisis",
    )
    assert all(nombre in spans for nombre in nombres)
    assert len({spans[n].context.trace_id for n in nombres}) == 1
    assert spans["worker_outbox.publicar_rabbitmq"].parent.span_id == spans["outbox.insertar_evento"].context.span_id
    assert spans["consumidor_ia.procesar"].parent.span_id == spans["worker_outbox.publicar_rabbitmq"].context.span_id
    assert spans["ia.analizar_transaccion"].parent.span_id == spans["consumidor_ia.procesar"].context.span_id
    assert spans["db.guardar_analisis"].parent.span_id == spans["consumidor_ia.procesar"].context.span_id


def test_mensaje_antiguo_sin_contexto_sigue_procesandose(monkeypatch):
    _, exportador = _tracer_de_prueba(monkeypatch)
    monkeypatch.setattr(consumidor, "crear_conexion", MagicMock())
    guardar = MagicMock()
    monkeypatch.setattr(consumidor, "guardar_analisis", guardar)
    mensaje = publicador.construir_mensaje_ia(_evento())
    assert "trace_context" not in mensaje

    canal = MagicMock()
    consumidor.callback(
        canal,
        SimpleNamespace(delivery_tag=8),
        None,
        json.dumps(mensaje).encode(),
    )
    guardar.assert_called_once()
    canal.basic_ack.assert_called_once_with(delivery_tag=8)
    canal.basic_nack.assert_not_called()
    spans = {span.name: span for span in exportador.get_finished_spans()}
    assert spans["consumidor_ia.procesar"].parent is None
