# 5. Observabilidad

Este documento describe qué información produce SmartBancs para diagnosticar problemas de rendimiento, degradación y fallos, y cómo se usa. Todo lo marcado **[MVP]** está implementado y es verificable en el código; **[PROD]** es propuesta no implementada; **[LIM]** es limitación actual.

---

## 5.1 Qué preguntas debe responder

La observabilidad del MVP está construida para responder, con datos y no con suposiciones:

| Pregunta | Dónde se responde hoy |
|---|---|
| ¿Cuántas transferencias se procesan? | `smartbancs_transacciones_total` |
| ¿Cuánto tardan? | `smartbancs_transferencia_duracion_seconds` (histograma) |
| ¿Qué porcentaje falla? | `smartbancs_transacciones_errores_total` frente al total |
| ¿Dónde ocurre el fallo? | Etiquetas `tipo` y `motivo` de errores y `smartbancs_http_errores_total` |
| ¿Se agota el pool de conexiones? | `smartbancs_db_pool_agotado_total`, `..._rechazos_total{motivo}`, `..._prestadas` |
| ¿Hay consultas lentas? | `smartbancs_db_sql_duracion_seconds{operacion}` y el log de PostgreSQL |
| ¿Hay deadlocks? | `smartbancs_db_deadlocks_total` + log `db_deadlock` + log del servidor |
| ¿Hay timeouts? | `smartbancs_db_timeouts_total`, `smartbancs_db_pool_timeouts_total` |
| ¿Se acumula trabajo asíncrono? | `smartbancs_outbox_eventos_total{resultado}` y la tabla `eventos_outbox`; **la profundidad de `cola_ia` no se mide** **[LIM]** |
| ¿Cuánto tarda la IA? | `smartbancs_ia_duracion_seconds` |
| ¿Qué servicios participaron en una operación? | Jaeger, siguiendo la traza distribuida |


### Justificación de las señales seleccionadas

El diseño se apoya en las cuatro señales clásicas de observabilidad de servicios: **latencia, tráfico, errores y saturación**. La latencia muestra el impacto percibido por el usuario; el tráfico permite saber si un cambio de comportamiento coincide con un aumento de carga; los errores muestran operaciones que ya no pueden completarse correctamente; y la saturación permite detectar que un recurso se está acercando a su límite antes de que el problema se convierta en fallos visibles.

Para la latencia se priorizan **percentiles** sobre promedios. Un promedio puede mantenerse aparentemente saludable aunque una fracción relevante de solicitudes tarde mucho más que el resto. El P95 permite observar precisamente esa cola lenta y, en producción, sería conveniente complementar con P50 y P99.

Los errores se clasifican mediante etiquetas como `tipo`, `motivo`, `status` y `sqlstate` porque **no todos los errores requieren la misma respuesta**. Por ejemplo, un `503` causado por agotamiento del pool apunta a capacidad o concurrencia; un `40P01` indica un deadlock; un `55P03` apunta a contención por bloqueos; y un `57014` indica que una sentencia excedió su tiempo máximo. La clasificación reduce el tiempo de diagnóstico y evita tratar síntomas distintos como si fueran el mismo incidente.


---

## 5.2 Los tres pilares en SmartBancs

- **Métricas** — agregados numéricos que responden *cuánto* y *con qué frecuencia*. Son
  la señal de guardia: dicen que algo va mal y en qué zona, pero no por qué en un caso
  concreto.
- **Logs** — eventos discretos con contexto, que responden *qué pasó exactamente en esta
  operación*: el SQLSTATE, el motivo del rechazo, el identificador de la transacción.
- **Trazas** — el recorrido de una operación concreta a través de los procesos, que
  responde *dónde se fue el tiempo* y *qué componente participó*. En SmartBancs es lo
  único que une una transferencia con su análisis de IA, separados por una tabla y una
  cola.

El diseño lo asume: las métricas detectan, las trazas localizan, los logs explican.

---

## 5.3 Logs estructurados

Los tres procesos emiten **JSON por stdout** **[MVP]**, pero con dos formateadores
distintos.

**`servicio_transacciones`**
([observabilidad/logging.py](../servicio_transacciones/app/observabilidad/logging.py)):
campos base `timestamp` (UTC ISO-8601), `nivel`, `logger`, `mensaje`; y estos campos
opcionales, incluidos solo si vienen informados:

`evento`, `trace_id`, `id_transaccion`, `duracion_ms`, `operacion`, `estado`, `error`,
`sqlstate`, `tipo_error`, `motivo`.

**`worker_outbox`** y **`consumidor_ia`**
([worker_outbox](../worker_outbox/app/observabilidad/logging.py),
[servicio_ia](../servicio_ia/app/observabilidad/logging.py)): campos base `nivel` y
`mensaje`, más `evento`, `trace_id`, `id_transaccion`, `tipo_error` y —solo en el
worker— `tipo_evento`.

**[LIM]**: los logs del worker y del consumidor **no incluyen timestamp ni nombre de
logger**. Al leerlos con `docker compose logs` el momento se puede recuperar del propio
Docker, pero si se centralizaran tal cual, quedarían sin marca temporal propia.

**[LIM]**: ambos procesos mezclan `print()` (`[OUTBOX] …`, `[IA] …`) con el logging
estructurado, así que una misma ejecución produce líneas JSON y líneas de texto plano.

---

## 5.4 Eventos registrados, por componente

Valores reales del campo `evento`:

**`servicio_transacciones`**

| Evento | Cuándo |
|---|---|
| `transferencia_completada` | Transferencia confirmada |
| `transferencia_rechazada` | Rechazo de negocio (incluye `motivo`) |
| `transferencia_reproducida` | Respuesta devuelta por idempotencia |
| `db_deadlock` | SQLSTATE `40P01` |
| `db_timeout` | SQLSTATE `57014` o `55P03` |
| `db_error` | Cualquier otro error de PostgreSQL |

Los tres primeros salen de `_registrar_resultado`; los tres últimos de
`_registrar_error_bd`, que añade `sqlstate`, `operacion` y `tipo_error`
([servicios/transferencias.py](../servicio_transacciones/app/servicios/transferencias.py)).

**[LIM]**: el agotamiento del pool **no genera log** en el camino activo. Se registra como
métrica desde el manejador de `PoolAgotado` en
[main.py](../servicio_transacciones/app/main.py), pero sin entrada de log. El único
`logger.warning` con evento `pool_agotado` que existe en el código está dentro de la
implementación asíncrona inactiva, así que nunca se ejecuta.

**`worker_outbox`**: `outbox_publicado`, `outbox_reintento`, `outbox_fallido`
([app/main.py](../worker_outbox/app/main.py)) y `rabbitmq_publicacion_error`
([publicador.py](../worker_outbox/app/publicador.py)).

**`consumidor_ia`**: `ia_analisis_completado`, `ia_analisis_error`,
`ia_persistencia_error` ([consumidor.py](../servicio_ia/app/consumidor.py)).

---

## 5.5 Correlación: dos identificadores distintos

SmartBancs maneja **dos** identificadores de correlación, y confundirlos lleva a búsquedas
infructuosas. Ambos existen en el código **[MVP]**:

| | `trace_id` de aplicación | Trace ID de OpenTelemetry |
|---|---|---|
| Origen | Cabecera `X-Trace-Id` de la petición, o `uuid4()` si no llega | Contexto W3C creado por la instrumentación de FastAPI o recibido en `traceparent` |
| Formato | UUID con guiones | 32 caracteres hexadecimales |
| Dónde vive | `ContextVar` del proceso ([trazabilidad.py](../servicio_transacciones/app/observabilidad/trazabilidad.py)) | Contexto de OpenTelemetry |
| Para qué sirve | Correlación funcional en **logs**; se devuelve al cliente en la cabecera de respuesta | Correlación de **spans** entre procesos; es lo que se busca en Jaeger |
| Cómo viaja entre servicios | Se copia dentro de `carga_util.trace_id` del evento outbox | Se inyecta en `carga_util.trace_context` y luego en el mensaje de RabbitMQ |

Consecuencia práctica: en los logs del `worker_outbox`, el campo `trace_id` es el
**identificador de aplicación** leído de la carga útil del evento; en los logs del
`consumidor_ia` y en el error de publicación de RabbitMQ, el campo `trace_id` es el
**trace ID de OpenTelemetry** en hexadecimal. Son dos espacios de identificadores
distintos conviviendo bajo el mismo nombre de campo **[LIM]**.

Además, `demo_web` genera su **propio `traceparent` W3C** antes de llamar al servicio de
transacciones, precisamente para conocer de antemano el trace ID y poder devolver al
navegador el enlace directo a Jaeger (`jaeger_trace_url`) sin consultar a nadie
([demo_web/app.py](../demo_web/app.py)).

El tercer identificador de correlación, y el más útil para un incidente, es el de negocio:
**`id_transaccion`**, presente en logs, en la tabla `transacciones`, en el evento outbox y
en `analisis_ia`.

---

## 5.6 Catálogo de métricas

Todas las métricas reales, con sus nombres y etiquetas exactos **[MVP]**. La etiqueta
`worker` es el PID del proceso Uvicorn.

### `servicio_transacciones` — negocio

| Métrica | Tipo | Etiquetas | Qué mide | Utilidad |
|---|---|---|---|---|
| `smartbancs_transacciones_total` | Counter | `estado` | Transferencias procesadas por estado (`COMPLETADA` / `RECHAZADA`) | Volumen, TPS y proporción de rechazos |
| `smartbancs_transacciones_reproducidas_total` | Counter | — | Respuestas devueltas por idempotencia | Detecta clientes reintentando |
| `smartbancs_transacciones_errores_total` | Counter | `tipo` (`deadlock`, `timeout`, `db_error`) | Errores al procesar | Clasifica la causa técnica |
| `smartbancs_transferencia_duracion_seconds` | Histogram | — | Tiempo total de procesamiento | Latencia percibida; buckets 0.01 · 0.025 · 0.05 · 0.1 · 0.25 · 0.5 · 1 · 2 · 5 s |
| `smartbancs_http_errores_total` | Counter | `worker`, `endpoint`, `status`, `tipo`, `motivo` | Errores HTTP de `POST /transacciones` | Distingue 404/409/422/500/503 y su causa |

### `servicio_transacciones` — base de datos

| Métrica | Tipo | Etiquetas | Qué mide |
|---|---|---|---|
| `smartbancs_db_operaciones_total` | Counter | `operacion`, `resultado` | Operaciones de BD por resultado |
| `smartbancs_db_operacion_duracion_seconds` | Histogram | `operacion` | Duración de operaciones de BD |
| `smartbancs_db_sql_duracion_seconds` | Histogram | `worker`, `operacion` | Duración de cada función SQL del repositorio, incluido el fetch |
| `smartbancs_db_lock_wait_seconds` | Histogram | — | Espera al bloquear cuentas |
| `smartbancs_db_deadlocks_total` | Counter | — | Deadlocks detectados (`40P01`) |
| `smartbancs_db_timeouts_total` | Counter | — | Timeouts detectados (`57014`, `55P03`) |
| `smartbancs_db_reintentos_total` | Counter | `sqlstate` | Reintentos completos de transferencia |
| `smartbancs_db_transaccion_duracion_seconds` | Histogram | — | Duración de cada intento, incluida la adquisición de conexión |

### `servicio_transacciones` — pool y threadpool

| Métrica | Tipo | Etiquetas | Qué mide |
|---|---|---|---|
| `smartbancs_db_pool_prestadas` | Gauge | — | Conexiones prestadas en este momento |
| `smartbancs_db_pool_adquisiciones_total` | Counter | — | Conexiones adquiridas |
| `smartbancs_db_pool_agotado_total` | Counter | — | Solicitudes sin conexión disponible |
| `smartbancs_db_pool_rechazos_total` | Counter | `motivo` (`cola_llena`, `timeout`, `pool`) | Rechazos del pool por causa |
| `smartbancs_db_pool_timeouts_total` | Counter | — | Esperas del pool agotadas |
| `smartbancs_db_pool_espera_seconds` | Histogram | — | Tiempo hasta obtener la conexión |
| `smartbancs_db_pool_semaforo_wait_seconds` | Histogram | `worker` | Espera del semáforo de préstamos |
| `smartbancs_db_pool_getconn_seconds` | Histogram | `worker` | Tiempo dentro de `getconn` |
| `smartbancs_db_pool_putconn_seconds` | Histogram | `worker` | Tiempo dentro de `putconn` |
| `smartbancs_threadpool_queue_seconds` | Histogram | `worker` | Desde el middleware hasta entrar en la ruta síncrona |
| `smartbancs_threadpool_tokens_total` | Gauge | `worker` | Tokens del limitador AnyIO |
| `smartbancs_threadpool_tokens_borrowed` | Gauge | `worker` | Tokens prestados |
| `smartbancs_threadpool_tasks_waiting` | Gauge | `worker` | Tareas esperando token |
| `smartbancs_ruta_transaccion_duracion_seconds` | Histogram | `worker` | Duración dentro de la función del endpoint |

### `worker_outbox`

| Métrica | Tipo | Etiquetas | Qué mide |
|---|---|---|---|
| `smartbancs_outbox_eventos_total` | Counter | `resultado` (`publicado`, `reintento`, `fallido`) | Resultado del procesamiento de eventos |
| `smartbancs_rabbitmq_publicaciones_total` | Counter | `resultado` (`ok`, `error`) | Publicaciones en RabbitMQ |
| `smartbancs_rabbitmq_publicacion_duracion_seconds` | Histogram | — | Duración de `basic_publish` |

### `consumidor_ia`

| Métrica | Tipo | Etiquetas | Qué mide |
|---|---|---|---|
| `smartbancs_ia_mensajes_total` | Counter | `resultado` (`procesado`, `rechazado`) | Mensajes consumidos |
| `smartbancs_ia_analisis_total` | Counter | `resultado` (`ok`, `error`) | Análisis ejecutados |
| `smartbancs_ia_duracion_seconds` | Histogram | — | Duración del análisis |
| `smartbancs_ia_persistencia_duracion_seconds` | Histogram | — | Duración de la escritura en `analisis_ia` |

---

## 5.7 Latencia, percentiles y throughput

**Latencia [MVP].** `smartbancs_transferencia_duracion_seconds` es un histograma con
buckets 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2 y 5 segundos. Los buckets están elegidos
para el rango esperado de una transferencia; una latencia real muy por encima de 5 s solo
se vería en `+Inf`, sin resolución.

**P95.** La consulta que usa el dashboard, tal cual:

```promql
1000 * histogram_quantile(0.95, sum by (le) (rate(smartbancs_transferencia_duracion_seconds_bucket[5m])))
```

`rate(..._bucket[5m])` obtiene la tasa por bucket, `sum by (le)` agrega respetando la
dimensión del histograma, `histogram_quantile(0.95, …)` interpola el percentil 95 y el
`1000 *` convierte **segundos a milisegundos**, que es como está etiquetado el panel.

P50 y P99 se obtendrían cambiando el primer argumento; **el dashboard actual solo
configura P95** **[LIM]**. El percentil está limitado por los bucket: con estos límites,
un P99 cercano al borde superior es aproximado.

**Throughput.** No existe una métrica "TPS": **se deriva** del contador.

```promql
sum(rate(smartbancs_transacciones_total[1m]))
```

**Tasa de error.** El dashboard la calcula como porcentaje, protegiendo la división:

```promql
100 * (sum(rate(smartbancs_transacciones_errores_total[5m])) or vector(0))
    / clamp_min((sum(rate(smartbancs_transacciones_total[5m])) or vector(0)), 1e-9)
```

**[LIM] importante sobre los contadores.** `servicio_transacciones` corre con 5 workers de
Uvicorn y **sin el modo multiproceso de `prometheus_client`**: cada respuesta de
`/metrics` proviene del registro del worker que atendió ese scrape. En consecuencia, los
paneles de contador acumulado (`sum(smartbancs_transacciones_total)`) fluctúan entre
scrapes según qué worker respondió, y no representan el total del servicio. Los paneles
basados en `rate(...)` sobre ventanas amplias amortiguan el efecto, pero no lo eliminan.
Está descrito en
[OBSERVABILIDAD_RENDIMIENTO.md](../servicio_transacciones/OBSERVABILIDAD_RENDIMIENTO.md).

---

## 5.8 Diagnóstico por capas dentro del servicio

La instrumentación está deliberadamente estratificada, de fuera hacia dentro:

```text
smartbancs_transferencia_duracion_seconds   ← tiempo total percibido
  └─ smartbancs_threadpool_queue_seconds    ← ¿espera antes de ejecutarse?  (saturación de workers)
     └─ smartbancs_ruta_transaccion_duracion_seconds  ← tiempo dentro del endpoint
        └─ smartbancs_db_pool_espera_seconds / _semaforo_wait  ← ¿espera por conexión?
           └─ smartbancs_db_sql_duracion_seconds{operacion}    ← ¿qué SQL concreto?
              └─ smartbancs_db_lock_wait_seconds               ← ¿espera por bloqueos?
```

Si el P95 total sube, esta cadena dice **en qué escalón** se fue el tiempo, que es
exactamente la pregunta que un dashboard de latencia agregada no responde.

Las etiquetas `operacion` de `smartbancs_db_sql_duracion_seconds` son las reales del
repositorio: `lectura_idempotencia`, `insert_transaccion`, `debito`, `credito`,
`insert_movimiento`, `update_transaccion`, `outbox`, `bloquear_cuentas_fallback`, más
`commit` y `rollback` medidos al cerrar la transacción. `smartbancs_db_operaciones_total`
y `smartbancs_db_operacion_duracion_seconds` se emiten hoy con un único valor de
`operacion`: **`bloquear_cuentas`**, que es la ruta de clasificación del rechazo **[LIM]**.

Medir por operación es lo que permite distinguir "la base de datos está lenta" de "el
`UPDATE` de saldos espera por bloqueos mientras el resto va bien".

---

## 5.9 Pool de conexiones

El pool es el punto donde la saturación se vuelve visible antes de convertirse en errores
del usuario.

| Síntoma observable | Métrica | Lectura |
|---|---|---|
| Conexiones en uso | `smartbancs_db_pool_prestadas` | Si se mantiene en el máximo (15 por worker), el pool es el límite |
| Espera por conexión | `smartbancs_db_pool_espera_seconds`, `..._semaforo_wait_seconds` | Espera creciente antes de que aparezcan errores |
| Rechazo por cola llena | `smartbancs_db_pool_rechazos_total{motivo="cola_llena"}` | Llegó más tráfico del que la cola corta admite |
| Rechazo por plazo agotado | `..._rechazos_total{motivo="timeout"}`, `smartbancs_db_pool_timeouts_total` | Se superaron los 50 ms de espera |
| Agotamiento total | `smartbancs_db_pool_agotado_total` | Cada incremento es un **503** devuelto al cliente |

Secuencia típica de degradación, en este orden: sube `db_pool_espera_seconds` → sube
`threadpool_tasks_waiting` → aparecen rechazos del pool → aparecen 503 en
`smartbancs_http_errores_total{status="503",tipo="pool"}` → sube el P95. Ver el síntoma
temprano evita diagnosticar tarde.

---

## 5.10 Deadlocks

Hay que distinguir **de dónde viene la señal** **[MVP]**:

1. **PostgreSQL detecta** el deadlock (con `deadlock_timeout=200ms` configurado en el
   `command` del contenedor) y aborta una de las transacciones con SQLSTATE `40P01`.
2. **La aplicación lo observa**: `_registrar_error_bd` incrementa
   `smartbancs_db_deadlocks_total` y `smartbancs_transacciones_errores_total{tipo="deadlock"}`,
   y escribe el log `db_deadlock` con el SQLSTATE y la operación.
3. **Prometheus recoge** esa métrica en el siguiente scrape.

Es decir: **Prometheus no detecta deadlocks**; muestra un contador que la aplicación
incrementa al recibir el error de PostgreSQL. Si un deadlock ocurriera entre sesiones
ajenas al servicio, no aparecería en esa métrica.

Señales complementarias del servidor **[MVP]**: `log_lock_waits=on` hace que PostgreSQL
registre las esperas de bloqueo largas, y `log_min_duration_statement=250ms` registra las
sentencias lentas, ambas en el log del contenedor `postgres`. Esa es una fuente
independiente de la aplicación.

También hay una señal previa y continua: `smartbancs_db_lock_wait_seconds`, que mide la
espera al bloquear cuentas **aunque no llegue a producirse deadlock**. Contención creciente
se ve ahí antes de que aparezca un `40P01`.

---

## 5.11 Timeouts: cuatro cosas distintas

| Tipo | Dónde se define | Señal observable |
|---|---|---|
| **Espera del pool** (50 ms) | `DB_POOL_TIMEOUT_S` | `smartbancs_db_pool_timeouts_total`, `..._rechazos_total{motivo="timeout"}`, HTTP 503 con `tipo="pool"` |
| **`lock_timeout`** (800 ms) | `ALTER ROLE svc_banc_user` en `ddl.sql` | SQLSTATE `55P03` → `smartbancs_db_timeouts_total`, log `db_timeout`, 503 |
| **`statement_timeout`** (1500 ms) | `ALTER ROLE svc_banc_user` | SQLSTATE `57014` → mismas métricas que el anterior, con `sqlstate` distinto en el log |
| **HTTP saliente** | `httpx` en `demo_web` (10 s) y en el cliente de Bancs (2 s, sin uso) | Errores en los logs de `demo_web` |

**No existe timeout de IA**, porque el consumidor invoca el modelo en proceso y no por red
(ver [04_inteligencia_artificial.md](04_inteligencia_artificial.md)) **[MVP]**.

Los dos primeros se distinguen en el log por el campo `sqlstate`, y esa distinción importa:
`55P03` significa contención por bloqueos; `57014` significa que la sentencia entera fue
demasiado lenta.

---

## 5.12 Prometheus

Configuración real en
[monitoreo/prometheus/prometheus.yml](../monitoreo/prometheus/prometheus.yml) **[MVP]**:

- `scrape_interval: 5s` y `evaluation_interval: 5s`.
- Tres *jobs*, sin más:

| Job | Target | Endpoint |
|---|---|---|
| `servicio_transacciones` | `servicio_transacciones:8000` | `/metrics` (aplicación ASGI montada con `make_asgi_app()`) |
| `worker_outbox` | `worker_outbox:9101` | `start_http_server(METRICS_PORT)` |
| `consumidor_ia` | `consumidor_ia:9102` | `start_http_server(METRICS_PORT)` |

**No se recolecta**: `servicio_ia` (la API), `servicio_bancs`, `demo_web`, PostgreSQL ni
RabbitMQ **[LIM]**. No hay *exporter* de PostgreSQL ni de RabbitMQ en el repositorio.

**No hay `rule_files` ni sección `alerting`**: no existen reglas de alerta ni Alertmanager
**[LIM]**.

**[LIM]** de retención: el servicio `prometheus` **no declara volumen de datos** (solo
monta el fichero de configuración en modo lectura), de modo que las series viven dentro del
contenedor y **se pierden al recrearlo**. Grafana sí tiene volumen (`grafana_data`).

---

## 5.13 Grafana

**Aprovisionado por archivo, versionado en el repositorio** **[MVP]**:

- **Datasource**:
  [datasources/datasource.yml](../monitoreo/grafana/provisioning/datasources/datasource.yml)
  define Prometheus en `http://prometheus:9090`, `access: proxy`, marcado como
  predeterminado.
- **Proveedor de dashboards**:
  [dashboards/dashboards.yml](../monitoreo/grafana/provisioning/dashboards/dashboards.yml)
  carga desde `/etc/grafana/provisioning/dashboards` en la carpeta `SmartBancs`.
- **Dashboard**: `SmartBancs - Monitoreo-1789888009473.json`, con 20 paneles, en la misma
  carpeta. El volumen `./monitoreo/grafana/provisioning` se monta en el contenedor, así que
  el dashboard **no depende del volumen local de Grafana**: está en el repositorio.

**Punto a verificar [LIM]:** el JSON está exportado en el **esquema v2 de Grafana**
(`apiVersion: dashboard.grafana.app/v2`, `kind: Dashboard`, contenido bajo `spec`), no en
el modelo clásico de dashboard que el aprovisionamiento por archivos ha consumido
históricamente. Como la imagen usada es `grafana/grafana:latest`, su compatibilidad depende
de la versión concreta que se descargue. Conviene confirmar en un arranque limpio que el
dashboard aparece en la carpeta `SmartBancs`.

### Paneles reales

Los 20 paneles del dashboard, con su consulta tal como está guardada:

| Panel | Consulta |
|---|---|
| Transacciones por segundo | `sum(rate(smartbancs_transacciones_total[1m]))` |
| Tiempo de respuesta / Tiempo de las transacciones | `1000 * histogram_quantile(0.95, sum by (le) (rate(smartbancs_transferencia_duracion_seconds_bucket[...])))` |
| Porcentaje de errores | `100 * rate(errores) / clamp_min(rate(total), 1e-9)` |
| Transacciones recibidas | `sum(smartbancs_transacciones_total)` |
| Transacciones completadas | `sum(smartbancs_transacciones_total{estado="COMPLETADA"})` |
| Transacciones con problemas | `sum(...{estado="RECHAZADA"}) + sum(smartbancs_transacciones_errores_total)` |
| Errores encontrados | `sum by (tipo) (increase(smartbancs_transacciones_errores_total[$__rate_interval]))` |
| Donde ocurrieron los errores | `increase` de errores de transacciones, `smartbancs_outbox_eventos_total{resultado="fallido"}`, `smartbancs_rabbitmq_publicaciones_total{resultado="error"}` e `smartbancs_ia_analisis_total{resultado="error"}` |
| Errores de base de datos | `sum(smartbancs_transacciones_errores_total{tipo=~"db_error\|timeout\|deadlock"})` |
| Bloqueos de base de datos | `sum(smartbancs_db_deadlocks_total)` |
| Esperas de base de datos | `sum(smartbancs_db_timeouts_total)` |
| Tiempo de base de datos | `1000 * histogram_quantile(0.95, ... smartbancs_db_operacion_duracion_seconds_bucket{operacion="bloquear_cuentas"} ...)` |
| Mensajes enviados | `sum(rate(smartbancs_rabbitmq_publicaciones_total{resultado="ok"}[1m]))` |
| Mensajes procesados | `sum(rate(smartbancs_ia_mensajes_total{resultado="procesado"}[1m]))` |
| Consultas a IA | `sum(smartbancs_ia_analisis_total)` |
| Errores de IA | `sum(smartbancs_ia_analisis_total{resultado="error"})` |
| Tiempo de respuesta de IA | `1000 * (sum(smartbancs_ia_duracion_seconds_sum) / sum(smartbancs_ia_duracion_seconds_count))` |
| Tiempo del servicio de IA | `1000 * histogram_quantile(0.95, sum by (le) (rate(smartbancs_ia_duracion_seconds_bucket[5m])))` |

Además hay un panel de enlace **"Abrir Jaeger"** apuntando a `http://localhost:18086/`, y
paneles de agrupación sin consulta (Estado general, Movimiento de transacciones, Tiempos de
respuesta, Errores y problemas, Inteligencia artificial).

**[LIM]**: el dashboard **no cubre** el pool de conexiones, el threadpool,
`smartbancs_db_sql_duracion_seconds` por operación, `smartbancs_db_lock_wait_seconds`,
`smartbancs_http_errores_total` ni las transacciones reproducidas. Esas métricas existen y
se recolectan, pero hoy hay que consultarlas directamente en Prometheus.

---

## 5.14 OpenTelemetry

**Configuración [MVP]:** tres `TracerProvider` independientes, uno por proceso, todos con
exportador **OTLP sobre gRPC** hacia `http://jaeger:4317` mediante `BatchSpanProcessor`:

| Proceso | `service.name` | Archivo | Instrumentación automática |
|---|---|---|---|
| `servicio_transacciones` | `servicio_transacciones` | [opentelemetry.py](../servicio_transacciones/app/observabilidad/opentelemetry.py) | `FastAPIInstrumentor.instrument_app(app)` |
| `worker_outbox` | `worker_outbox` | [opentelemetry.py](../worker_outbox/app/observabilidad/opentelemetry.py) | — |
| `consumidor_ia` | `consumidor_ia` | [opentelemetry.py](../servicio_ia/app/observabilidad/opentelemetry.py) | — |

El servicio de transacciones permite desactivar el exportador con
`OTEL_TRACES_EXPORTER=none`, útil en pruebas: el `TracerProvider` sigue activo pero no
envía nada.

**[LIM]**: la API `servicio_ia` (puerto 8002) **no configura OpenTelemetry**; solo el
consumidor lo hace. Consultar un análisis no genera traza. `servicio_bancs` y `demo_web`
tampoco están instrumentados.

### Spans manuales

Nombres exactos, verificados en el código:

| Span | Proceso | Atributos destacados |
|---|---|---|
| `idempotencia.obtener_o_crear` | transacciones | — |
| `db.actualizar_saldos` | transacciones | `db.system.name`, `db.operation.name`, `db.duration_ms` |
| `db.clasificar_rechazo` | transacciones | — |
| `db.bloquear_cuentas` | transacciones | `db.lock_wait_ms`, `smartbancs.accounts_locked` |
| `db.registrar_movimientos` | transacciones | `smartbancs.movements` |
| `db.completar_transaccion` | transacciones | `db.operation.name` |
| `outbox.insertar_evento` | transacciones | `messaging.system`, `messaging.destination.name`, `smartbancs.outbox.event_type` |
| `worker_outbox.publicar_rabbitmq` | worker | `messaging.operation.name="publish"`, `smartbancs.event.type` |
| `consumidor_ia.procesar` | consumidor | `messaging.operation.name="process"`, `smartbancs.event.type` |
| `ia.analizar_transaccion` | consumidor | `gen_ai.operation.name`, `smartbancs.ai.model`, `smartbancs.ai.level` |
| `db.guardar_analisis` | consumidor | `db.system.name`, `db.operation.name`, `db.namespace` |

A estos se suman los spans HTTP que genera automáticamente la instrumentación de FastAPI
en el servicio de transacciones.

---

## 5.15 Traza distribuida y propagación de contexto

La cadena real, comprobada por
[tests/test_trazabilidad_distribuida.py](../tests/test_trazabilidad_distribuida.py)
**[MVP]**:

```text
[servicio_transacciones]  span HTTP (FastAPI)
      └─ idempotencia.obtener_o_crear
      └─ db.actualizar_saldos
      └─ db.registrar_movimientos
      └─ db.completar_transaccion
      └─ outbox.insertar_evento        ── inject(contexto) → carga_util.trace_context
                 │                         (persistido en PostgreSQL)
                 ▼
[worker_outbox]   worker_outbox.publicar_rabbitmq   ── extract(carga_util.trace_context)
                 │                                     inject(mensaje.trace_context)
                 ▼
[consumidor_ia]   consumidor_ia.procesar             ── extract(mensaje.trace_context)
                      ├─ ia.analizar_transaccion
                      └─ db.guardar_analisis
```

El mecanismo es **W3C Trace Context**, usando `inject()` y `extract()` del propagador de
OpenTelemetry:

1. El servicio de transacciones **inyecta** el contexto en un diccionario y lo escribe
   dentro de `carga_util.trace_context` **en la misma transacción** que la transferencia.
2. El worker lo **extrae** y lo usa como **contexto padre** de su span de publicación; el
   contexto sobrevive así al paso por una tabla de base de datos.
3. El worker **reinyecta** su propio contexto en el campo `trace_context` del mensaje de
   RabbitMQ (`construir_mensaje_ia`), no reenvía el anterior.
4. El consumidor lo **extrae** del mensaje y cuelga de ahí sus dos spans hijos.

La prueba verifica la relación padre/hijo exacta de cada eslabón, y también que **un
mensaje antiguo sin `trace_context` se sigue procesando**, simplemente iniciando una traza
nueva.

Esto es lo que aporta un valor real: una transferencia y el análisis de IA que ocurre
segundos después, en otro proceso y tras pasar por una tabla y una cola, aparecen en
**una sola traza**.

Precisión importante: **`servicio_ia` no aparece como proceso en las trazas**. El análisis
lo ejecuta `consumidor_ia` en su propio proceso, por lo que `ia.analizar_transaccion` es un
span de `consumidor_ia`, no de un cuarto servicio.

---

## 5.16 Jaeger

**Configuración [MVP]:** imagen `jaegertracing/all-in-one:latest` con
`COLLECTOR_OTLP_ENABLED=true`; recibe OTLP gRPC en el puerto interno `4317` y publica la
interfaz en `http://localhost:18086` (mapeo `18086:16686`).

Lo que permite hacer con lo que hay instrumentado:

- **Buscar por servicio**: `servicio_transacciones`, `worker_outbox`, `consumidor_ia`.
- **Abrir una traza concreta por su ID**, que es lo que hace `demo_web` al devolver
  `jaeger_trace_url` tras una transferencia.
- **Ver la jerarquía padre/hijo** de los spans y su duración relativa.
- **Ver los atributos** de cada span (`db.lock_wait_ms`, `smartbancs.accounts_locked`,
  `smartbancs.ai.level`, etc.).

Casos que el detalle de spans permite diagnosticar:

| Síntoma | Qué mirar en la traza |
|---|---|
| Latencia alta en la API | Qué span concreto domina la duración del span HTTP |
| Operación de BD lenta | Duración de `db.actualizar_saldos` frente a `db.registrar_movimientos` o `db.completar_transaccion` |
| Contención por bloqueos | Aparición de `db.clasificar_rechazo` y el atributo `db.lock_wait_ms` de `db.bloquear_cuentas` |
| Demora del worker | Diferencia temporal entre `outbox.insertar_evento` y `worker_outbox.publicar_rabbitmq`: es el retraso del sondeo o de la cola |
| Demora de la IA | Duración de `ia.analizar_transaccion` y de `db.guardar_analisis` |
| Ruptura del contexto | La traza termina en el servicio de transacciones y el análisis aparece como traza independiente |

**[LIM]**: Jaeger *all-in-one* almacena las trazas **en memoria**; al reiniciar el
contenedor se pierden. No es apto para investigar un incidente pasado.

---

## 5.17 PostgreSQL

### Lo configurado **[MVP]**

En el `command` del contenedor `postgres` de
[docker-compose.yml](../docker-compose.yml):

| Parámetro | Valor | Para qué |
|---|---|---|
| `shared_preload_libraries=pg_stat_statements` + `pg_stat_statements.track=all` | activo | Estadísticas acumuladas por sentencia |
| `log_lock_waits=on` | activo | Registra esperas de bloqueo largas |
| `deadlock_timeout=200ms` | 200 ms | Umbral de detección de deadlock |
| `log_min_duration_statement=250ms` | 250 ms | Registra sentencias que superan ese tiempo |
| `log_line_prefix='%m [%p] app=%a db=%d user=%u '` | activo | Cada línea lleva momento, PID, `application_name`, base y usuario |
| `max_connections=200` | 200 | Techo de conexiones |

La extensión `pg_stat_statements` también se crea en
[ddl.sql](../base_datos/ddl.sql). El `log_line_prefix` incluye `%a`, y las conexiones se
abren con `application_name` (`smartbancs`, `worker_outbox`, `consumidor_ia`), así que en
el log del servidor **se puede distinguir qué componente originó cada sentencia**.

**[LIM]**: no hay *exporter* de PostgreSQL. Nada de lo anterior llega a Prometheus ni a
Grafana: son herramientas de **diagnóstico manual**, consultadas por SQL o leyendo el log
del contenedor.

### Diagnóstico manual durante un incidente

**`pg_stat_activity`** — fotografía del momento: qué sesiones están activas, qué consulta
ejecutan, desde cuándo (`now() - query_start`), en qué estado (`active`,
`idle in transaction`) y **si están esperando** (`wait_event_type`, `wait_event`). Con
`application_name` se separa el tráfico del servicio transaccional del worker y del
consumidor. Sirve para responder "¿qué está corriendo ahora mismo y quién no avanza?".

**`pg_locks`** — quién bloquea a quién: qué bloqueos están concedidos (`granted = true`) y
cuáles esperan (`granted = false`), sobre qué relación o tupla y de qué modo. Cruzado con
`pg_stat_activity` por `pid`, identifica la **sesión bloqueante** y la cadena de espera.
Complementa a `smartbancs_db_lock_wait_seconds`, que dice *cuánto* se espera pero no *por
culpa de quién*.

**`pg_stat_statements`** — visión acumulada, no instantánea: qué sentencias se ejecutan más
veces, cuáles suman más tiempo total y cuál es su tiempo medio. Es la forma de encontrar la
consulta que, aun siendo rápida, domina la carga por frecuencia. Aquí es especialmente
útil porque el SQL está escrito a mano y es estable, de modo que las sentencias se agrupan
de forma limpia.

---

## 5.18 Diseño de observabilidad: qué señal indica qué

Respuesta directa al requisito del reto sobre qué información se utilizaría para
identificar problemas de rendimiento, degradación o fallos.

| Señal | Fuente | Qué indica | Acción |
|---|---|---|---|
| P95 de `smartbancs_transferencia_duracion_seconds` | Prometheus/Grafana **[MVP]** | Degradación percibida por el usuario | Descender por la cadena de 5.8 |
| `smartbancs_transacciones_total{estado="RECHAZADA"}` en aumento | Prometheus **[MVP]** | Problema funcional o de datos, no técnico | Revisar `motivo` en logs |
| `smartbancs_transacciones_errores_total{tipo}` | Prometheus **[MVP]** | Falla técnica, clasificada por causa | Según `tipo`: deadlock, timeout o error de BD |
| `smartbancs_http_errores_total{status,motivo}` | Prometheus **[MVP]** | Qué devuelve el endpoint y por qué | Distingue 503 de pool frente a 503 de saturación de BD |
| `smartbancs_db_pool_agotado_total` / `..._rechazos_total{motivo}` | Prometheus **[MVP]** | Falta de conexiones; cada incremento es un 503 | Revisar concurrencia y dimensionamiento del pool |
| `smartbancs_db_pool_prestadas` en el máximo | Prometheus **[MVP]** | El pool es el cuello de botella | Igual que el anterior |
| `smartbancs_db_lock_wait_seconds` creciente | Prometheus **[MVP]** | Contención sobre las mismas cuentas | `pg_locks` para identificar la sesión bloqueante |
| `smartbancs_db_deadlocks_total` | Prometheus + log `db_deadlock` **[MVP]** | Deadlock real detectado por PostgreSQL | Revisar orden de actualización y carga concurrente |
| `smartbancs_db_timeouts_total` con `sqlstate` en logs | Prometheus + logs **[MVP]** | `55P03` = espera por bloqueo; `57014` = sentencia lenta | Distinguir contención de consulta costosa |
| `smartbancs_db_sql_duracion_seconds{operacion}` | Prometheus **[MVP]** | Qué operación SQL concreta se degrada | `pg_stat_statements` para el detalle |
| `smartbancs_outbox_eventos_total{resultado="fallido"}` | Prometheus **[MVP]** | Eventos que agotaron reintentos | Revisar RabbitMQ y la tabla `eventos_outbox` |
| `smartbancs_rabbitmq_publicaciones_total{resultado="error"}` | Prometheus **[MVP]** | Problemas de publicación | Estado del broker |
| `smartbancs_ia_mensajes_total{resultado="rechazado"}` | Prometheus **[MVP]** | Mensajes descartados: análisis perdidos | Revisar logs `ia_analisis_error` / `ia_persistencia_error` |
| P95 de `smartbancs_ia_duracion_seconds` | Prometheus **[MVP]** | Coste del análisis | No afecta a la transferencia, pero sí al retraso del análisis |
| Traza con span dominante | Jaeger **[MVP]** | Qué componente concreto consume el tiempo | Ir al log de ese componente |
| Sentencias lentas y esperas de bloqueo | Log de PostgreSQL **[MVP]** | Señal independiente de la aplicación | Contrastar con las métricas |
| Consultas activas y bloqueos | `pg_stat_activity`, `pg_locks` **[MVP, manual]** | Estado en vivo del servidor | Identificar sesión bloqueante |
| **Profundidad de `cola_ia`** | **No medida** **[LIM]** | Acumulación de trabajo asíncrono | **[PROD]**: exporter de RabbitMQ o consulta a su API |
| **Antigüedad del evento outbox más viejo** | **No medida** **[LIM]** | Retraso real del pipeline asíncrono | **[PROD]**: consulta periódica sobre `eventos_outbox` |
| **CPU / memoria por contenedor** | **No medida** **[LIM]** | Saturación de recursos | **[PROD]**: cAdvisor o node-exporter |

Las tres últimas filas son huecos reales: el sistema mide bien lo que ocurre **dentro** de
sus procesos y no mide lo que se **acumula entre** ellos.

---

## 5.19 Correlación durante un incidente

Procedimiento que conecta las herramientas, en el orden en que realmente se usan:

1. **Grafana** muestra la degradación: sube el P95, sube el porcentaje de errores o
   aparecen deadlocks en "Bloqueos de base de datos".
2. **Acotar la ventana temporal**: minuto de inicio y patrón (escalón o pendiente).
3. **Clasificar el error** con "Errores encontrados" (`sum by (tipo)`) y, si hace falta,
   consultando `smartbancs_http_errores_total` por `status` y `motivo` directamente en
   Prometheus, ya que no tiene panel.
4. **Obtener un caso concreto**: un `id_transaccion` de los logs del servicio en esa
   ventana, o el `otel_trace_id` que `demo_web` devuelve en la respuesta de la
   transferencia.
5. **Abrir Jaeger** en `http://localhost:18086` con ese trace ID, o buscando por servicio y
   filtrando por duración.
6. **Identificar el span dominante**: si es `db.actualizar_saldos`, el tiempo se va en el
   `UPDATE`; si aparece `db.clasificar_rechazo`, hubo `UPDATE` sin filas afectadas; si el
   hueco está entre `outbox.insertar_evento` y `worker_outbox.publicar_rabbitmq`, el
   problema es el retraso del pipeline asíncrono, no la transferencia.
7. **Leer los logs** de ese componente filtrando por `id_transaccion` (correlación de
   negocio) o por `trace_id`, teniendo presente cuál de los dos identificadores usa cada
   proceso (5.5). El campo `sqlstate` da la causa exacta.
8. **Si apunta a la base de datos**, pasar al diagnóstico manual: `pg_stat_activity` para
   ver qué corre ahora, `pg_locks` para la cadena de bloqueos y `pg_stat_statements` para
   el coste acumulado, contrastando con el log del servidor (`log_lock_waits`,
   `log_min_duration_statement`).

El uso de este procedimiento sobre un caso concreto se desarrolla en
[06_incidente_critico.md](06_incidente_critico.md).

---

## 5.20 Alertas

**No existe ninguna alerta implementada** **[LIM]**: no hay Alertmanager, no hay
`rule_files` en la configuración de Prometheus, no hay sección `alerting` y el dashboard no
define alertas de Grafana. Hoy la detección es **visual**: alguien tiene que estar mirando
el dashboard.

**[PROD]** — alertas que la instrumentación existente ya permitiría definir sin añadir una
sola métrica:

| Alerta propuesta | Basada en |
|---|---|
| Latencia degradada | P95 de `smartbancs_transferencia_duracion_seconds` por encima del umbral acordado, sostenido varios minutos |
| Tasa de error elevada | Porcentaje de `smartbancs_transacciones_errores_total` sobre el total |
| Agotamiento del pool | Cualquier incremento sostenido de `smartbancs_db_pool_agotado_total` |
| Deadlocks | Incremento de `smartbancs_db_deadlocks_total` sobre la línea base |
| Eventos outbox fallidos | `smartbancs_outbox_eventos_total{resultado="fallido"}` creciendo |
| Mensajes de IA rechazados | `smartbancs_ia_mensajes_total{resultado="rechazado"}` creciendo (son análisis perdidos) |
| Backlog asíncrono | Requiere primero **medir** la profundidad de cola y la antigüedad del evento más viejo |

Los umbrales concretos deben calibrarse con datos reales; fijarlos aquí sin medición previa
sería inventarlos.

---

## 5.21 Limitaciones actuales

1. **Sin alertas ni Alertmanager**: la detección es visual **[LIM]**.
2. **Sin exporter de PostgreSQL**: `pg_stat_activity`, `pg_locks` y `pg_stat_statements`
   solo son accesibles manualmente **[LIM]**.
3. **Sin exporter de RabbitMQ**: la profundidad de cola, la tasa de consumo y la antigüedad
   de los mensajes no se miden **[LIM]**.
4. **Sin métricas de infraestructura**: ni CPU, ni memoria, ni disco por contenedor
   **[LIM]**.
5. **Métricas no agregadas entre los 5 workers de Uvicorn**: cada scrape refleja un solo
   proceso **[LIM]** (5.7).
6. **Retención de Prometheus efímera**: sin volumen de datos, las series se pierden al
   recrear el contenedor **[LIM]**.
7. **Trazas en memoria**: Jaeger *all-in-one* las pierde al reiniciar **[LIM]**.
8. **Logs sin centralizar**: solo `docker compose logs`; no hay agregador ni búsqueda, y no
   se pueden correlacionar logs de varios servicios sin filtrar a mano **[LIM]**.
9. **Cobertura parcial de métricas**: `servicio_ia` (API), `servicio_bancs` y `demo_web` no
   exponen métricas ni están en los targets **[LIM]**.
10. **`servicio_ia`, `servicio_bancs` y `demo_web` sin trazas** **[LIM]**.
11. **Dos identificadores llamados `trace_id`** con semántica distinta según el componente
    **[LIM]** (5.5).
12. **El dashboard no cubre pool, threadpool ni SQL por operación**, pese a que esas
    métricas se recolectan **[LIM]**.
13. **Formato de log desigual** entre servicios: sin timestamp en worker y consumidor, y
    mezcla con `print()` **[LIM]**.
14. **El agotamiento del pool no deja log**, solo métrica **[LIM]**.

Ninguna de estas limitaciones invalida el diagnóstico: cubren lo esencial del camino
crítico. Pero conviene saber dónde están los huecos antes de necesitarlos.

---

## 5.22 Evolución a producción **[PROD]**

Breve, y en orden de rentabilidad:

| Área | Propuesta |
|---|---|
| **Alertas** | Alertmanager con las reglas de 5.20 y una ruta de notificación definida |
| **Exporters** | `postgres_exporter` y el exporter de RabbitMQ: cubren los dos huecos más grandes (contención de BD y backlog de cola) |
| **Métricas multiproceso** | Configurar `PROMETHEUS_MULTIPROC_DIR` para que `/metrics` agregue los 5 workers |
| **Centralización de logs** | Agregador con búsqueda, y un único formato JSON con timestamp en los tres servicios |
| **Retención** | Volumen persistente para Prometheus y almacenamiento real de trazas en Jaeger, con retención acordada |
| **SLO** | Definir objetivos explícitos (latencia y disponibilidad de la transferencia) y alertar sobre consumo de presupuesto de error, no sobre umbrales sueltos |
| **Dashboards separados** | Uno operativo (negocio) y otro técnico (pool, SQL, threadpool, outbox) |
| **Seguridad** | Grafana y Prometheus no deberían quedar expuestos sin autenticación, y las credenciales no deberían viajar en variables de entorno en claro |
