# Instrumentación de rendimiento de transferencias

Las métricas nuevas usan la etiqueta `worker` con el PID del proceso Uvicorn. No usan IDs de peticiones, cuentas ni transacciones.

| Métrica | Qué mide |
| --- | --- |
| `smartbancs_threadpool_queue_seconds` | Desde la entrada al middleware HTTP hasta la primera línea de la función síncrona. Aproxima la cola de AnyIO, pero incluye validación de FastAPI y otro trabajo ASGI previo. |
| `smartbancs_threadpool_tokens_total`, `_borrowed`, `smartbancs_threadpool_tasks_waiting` | Instantáneas del limitador AnyIO al entrar y salir del middleware. No son un muestreo continuo. |
| `smartbancs_db_pool_semaforo_wait_seconds` | Espera del semáforo de préstamos, incluidos rechazos por timeout o cola llena. |
| `smartbancs_db_pool_getconn_seconds` | Tiempo dentro de `ThreadedConnectionPool.getconn()`, que puede incluir mutex y apertura de conexión. |
| `smartbancs_db_pool_putconn_seconds` | Tiempo dentro de `ThreadedConnectionPool.putconn()`, que puede incluir cierre de conexión. |
| `smartbancs_db_sql_duracion_seconds{operacion=...}` | Duración de funciones SQL del repositorio, incluido fetch y adaptación local; también mide salida del contexto transaccional como `commit` o `rollback` y la lectura posterior al commit. |
| `smartbancs_ruta_transaccion_duracion_seconds` | Tiempo dentro de la función síncrona del endpoint, incluida adquisición de conexión. |

`smartbancs_db_transaccion_duracion_seconds` se conserva por compatibilidad. Su nombre es histórico: incluye adquisición de conexión y trabajo posterior al commit. `lectura_post_commit` contiene una llamada que también se registra como `lectura_idempotencia`; no se deben sumar ambas series.

La API pública de `ThreadedConnectionPool` no indica si una adquisición reutilizó una conexión o abrió una nueva. Se omite esa clasificación para no depender de sus atributos privados. La exposición Prometheus multiproceso no está configurada: cada respuesta de `/metrics` refleja el worker que atendió esa petición, aunque la etiqueta permita identificarlo. No se deben sumar scrapes alternos como si fueran una serie completa de ambos workers.