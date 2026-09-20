from prometheus_client import Counter, Histogram

IA_ANALISIS_TOTAL = Counter(
    "smartbancs_ia_analisis_total", "Analisis IA por resultado", ["resultado"]
)
IA_DURACION_SECONDS = Histogram(
    "smartbancs_ia_duracion_seconds", "Duracion del analisis IA"
)
IA_MENSAJES_TOTAL = Counter(
    "smartbancs_ia_mensajes_total", "Mensajes IA por resultado", ["resultado"]
)
IA_PERSISTENCIA_DURACION_SECONDS = Histogram(
    "smartbancs_ia_persistencia_duracion_seconds", "Duracion de persistencia IA"
)
