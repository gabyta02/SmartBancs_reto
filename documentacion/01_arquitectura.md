# 1. Arquitectura del sistema

Este documento describe la arquitectura de SmartBancs tal como está implementada en el
repositorio. Cubre la visión general, los componentes, el flujo síncrono de una
transferencia, el flujo asíncrono posterior, el patrón Transactional Outbox y los
límites conocidos del MVP.

Estilo arquitectónico
SmartBancs combina microservicios con una arquitectura orientada a eventos. Con un enfoque híbrido: los servicios se despliegan por separado, pero en este MVP comparten una misma instancia de PostgreSQL. [LIM] En producción cada servicio tendría su propio almacenamiento.

Las decisiones técnicas y la justificación del stack se detallan en
[09_decisiones_tecnicas.md](09_decisiones_tecnicas.md).

## 1.0 Convención de lectura

Cada afirmación relevante se clasifica con una de estas tres etiquetas:

| Etiqueta | Significado |
|---|---|
| **[MVP]** | Implementado y verificable en el código del repositorio. |
| **[PROD]** | Propuesta para un despliegue productivo. No está implementada. |
| **[LIM]** | Limitación actual conocida del MVP. |

---

## 1.1 Visión general

SmartBancs es un MVP compuesto por servicios independientes que se comunican de dos
formas distintas **[MVP]**:

- **síncrona (HTTP)** para el camino crítico de una transferencia: el cliente recibe la
  respuesta financiera sin esperar ningún procesamiento posterior;
- **asíncrona (Transactional Outbox + RabbitMQ)** para todo lo que puede ocurrir después:
  análisis de IA y publicación de eventos.

La frontera entre ambos mundos es una única transacción de PostgreSQL: dentro de ella se
mueven los saldos, se registran los movimientos y se deja escrito el evento de salida. Al
confirmar (`COMMIT`), el resultado financiero y la intención de publicar el evento son
atómicamente consistentes.

```text
                        ┌───────────────────────────────┐
   Navegador ─────────► │ demo_web (8080)               │
                        │ fachada, sin lógica de negocio│
                        └───────────┬───────────────────┘
                                    │ HTTP (síncrono)
                                    ▼
                        ┌───────────────────────────────┐
                        │ servicio_transacciones (8000) │
                        │ validación · idempotencia ·   │
                        │ saldos · movimientos · outbox │
                        └───────────┬───────────────────┘
                                    │ 1 transacción ACID
                                    ▼
                        ┌───────────────────────────────┐
                        │ PostgreSQL 16                 │
                        │ cuentas · transacciones ·     │
                        │ movimientos · eventos_outbox ·│
                        │ analisis_ia                   │
                        └───────────┬───────────────────┘
                                    │ sondeo cada 1 s
                                    │ FOR UPDATE SKIP LOCKED
                                    ▼
                        ┌───────────────────────────────┐
                        │ worker_outbox                 │
                        └───────────┬───────────────────┘
                                    │ publish (cola durable)
                                    ▼
                        ┌───────────────────────────────┐
                        │ RabbitMQ (cola_ia)            │
                        └───────────┬───────────────────┘
                                    │ consume (prefetch=1, ack manual)
                                    ▼
                        ┌───────────────────────────────┐
                        │ consumidor_ia                 │
                        │ analiza y persiste analisis_ia│
                        └───────────────────────────────┘

   servicio_ia (8002) expone el análisis ya persistido por HTTP.
   servicio_bancs (8001) simula el core bancario legado.
```

Todo el entorno se levanta con Docker Compose **[MVP]**
([docker-compose.yml](../docker-compose.yml)).

---

## 1.2 Componentes

| Componente | Rol | Implementación | Puerto |
|---|---|---|---|
| `demo_web` | Fachada de demostración: resuelve números de cuenta, reenvía la transferencia, consulta el análisis de IA y ejecuta el ETL. No contiene lógica de negocio. | [demo_web/app.py](../demo_web/app.py) | 8080 |
| `servicio_transacciones` | Núcleo transaccional: valida, aplica idempotencia, mueve saldos, registra movimientos y escribe el evento outbox. | [servicio_transacciones/app](../servicio_transacciones/app) | 8000 |
| `worker_outbox` | Proceso de fondo: reclama eventos `PENDIENTE` y los publica en RabbitMQ. | [worker_outbox/app/main.py](../worker_outbox/app/main.py) | métricas 9101 |
| `consumidor_ia` | Consume `cola_ia`, ejecuta el análisis y persiste el resultado en `analisis_ia`. | [servicio_ia/app/consumidor.py](../servicio_ia/app/consumidor.py) | métricas 9102 |
| `servicio_ia` | API de consulta del análisis (`/analisis/{id}/usuario` y `/admin`) y endpoint directo `/analizar`. | [servicio_ia/app/main.py](../servicio_ia/app/main.py) | 8002 |
| `servicio_bancs` | Mock del core bancario legado: `POST /eventos` y `GET /health`. | [servicio_bancs/app/main.py](../servicio_bancs/app/main.py) | 8001 |
| `postgres` | Base de datos transaccional (imagen `postgres:16-alpine`). Inicializa rol, DDL y datos de prueba. | [base_datos/](../base_datos/) | 5432 |
| `rabbitmq` | Intermediario de mensajes (`rabbitmq:3-management`). | imagen oficial | 5672 / 15672 |
| `prometheus` | Recolección de métricas. | [monitoreo/prometheus/prometheus.yml](../monitoreo/prometheus/prometheus.yml) | 9090 |
| `grafana` | Dashboards aprovisionados por archivo. | [monitoreo/grafana/provisioning](../monitoreo/grafana/provisioning) | 3000 |
| `jaeger` | Recepción y visualización de trazas (OTLP gRPC en `jaeger:4317`). | imagen `all-in-one` | 18086 → 16686 |

Notas de despliegue:

- El servicio de transacciones se ejecuta con varios workers de Uvicorn
  (`UVICORN_WORKERS`, valor por defecto **5**) y cada worker mantiene su propio pool de
  conexiones **[MVP]** ([Dockerfile](../servicio_transacciones/Dockerfile)).
- PostgreSQL arranca con `pg_stat_statements`, `log_lock_waits=on`,
  `deadlock_timeout=200ms`, `log_min_duration_statement=250ms` y `max_connections=200`
  **[MVP]**.
- La inicialización de la base de datos monta tres scripts en
  `docker-entrypoint-initdb.d`: creación del rol de aplicación
  ([init_user.sh](../base_datos/init_user.sh)), esquema
  ([ddl.sql](../base_datos/ddl.sql)) y datos de prueba ([dml.sql](../base_datos/dml.sql))
  **[MVP]**.
- Todos los servicios que requieren comunicación interna, incluido
  `servicio_bancs`, están asociados a la red Docker
  `smartbanks_network`, permitiendo resolución DNS por nombre de
  servicio y comunicación interna entre contenedores **[MVP]**.

---

## 1.3 Flujo síncrono: procesamiento de una transferencia

El camino crítico termina en el `COMMIT` de una sola transacción de base de datos. Nada
de lo que ocurre después (IA, publicación de eventos) forma parte de la respuesta.

```text
POST /demo/transacciones              (demo_web)
  │  resuelve numero_cuenta -> UUID  vía GET /cuentas/{numero}/resolver
  │  genera traceparent W3C propio
  ▼
POST /transacciones                   (servicio_transacciones)
  │  middleware: X-Trace-Id + muestreo del threadpool
  │  validación Pydantic (monto > 0, divisa de 3 letras, cuentas distintas)
  │  hash de solicitud SHA-256 (origen|destino|monto|divisa)
  ▼
préstamo de conexión del pool  ──── sin conexión disponible ──► 503 + Retry-After
  ▼
BEGIN
  ├─ obtener_o_crear_transaccion       SELECT por idempotencia + INSERT bajo SAVEPOINT
  │     · clave ya usada con el mismo hash ──► respuesta reproducida
  │     · clave ya usada con otro hash    ──► 409 ConflictoIdempotencia
  ├─ SAVEPOINT sp_transferencia_saldos
  ├─ UPDATE saldos en orden de UUID      débito condicionado a saldo, estado y divisa
  │     · si un UPDATE no afecta filas ──► ROLLBACK al savepoint y clasificación
  │       con SELECT ... FOR NO KEY UPDATE (motivo del rechazo)
  ├─ INSERT movimientos (DEBITO y CREDITO, con saldo resultante)
  ├─ UPDATE transacciones -> COMPLETADA (+ completado_en)
  └─ INSERT eventos_outbox (TRANSFERENCIA_COMPLETADA, estado PENDIENTE)
COMMIT
  ▼
201 Created con el estado de la transacción
```

Puntos verificables del flujo **[MVP]**:

- **Idempotencia por clave.** `id_idempotencia` tiene restricción `UNIQUE` en la tabla
  `transacciones`. El servicio consulta, y si no existe inserta bajo un `SAVEPOINT`; ante
  una `UniqueViolation` concurrente vuelve al savepoint y relee la fila ganadora. La
  respuesta incluye `reproducida: true` cuando se devuelve un resultado previo
  ([idempotencia.py](../servicio_transacciones/app/servicios/idempotencia.py)).
- **Orden determinista de bloqueo.** Las cuentas se actualizan ordenadas por UUID
  (`sorted(...)`), y la consulta de respaldo usa `ORDER BY id_cuenta ... FOR NO KEY UPDATE`.
  Esto reduce los deadlocks entre transferencias cruzadas.
- **Camino rápido sin bloqueo explícito.** El caso normal no hace `SELECT ... FOR UPDATE`:
  el débito es un `UPDATE ... WHERE saldo >= monto AND estado = 'ACTIVA' AND divisa = %s`
  que se autovalida. Solo cuando el `UPDATE` no afecta filas se toma el camino de
  clasificación con bloqueo explícito para determinar el motivo del rechazo
  (`SALDO_INSUFICIENTE`, `CUENTA_ORIGEN_NO_ACTIVA`, `CUENTA_DESTINO_NO_ACTIVA`,
  `DIVISA_NO_COMPATIBLE`).
- **Reintentos acotados.** Ante `40P01` (deadlock), `55P03` (lock_timeout) o `57014`
  (statement_timeout) se reintenta la operación completa hasta `max_reintentos` (3) con
  backoff exponencial y jitter. Agotados los reintentos se responde **503** con
  `Retry-After: 1` e indicación de reintentar con la misma clave de idempotencia
  ([transferencias.py](../servicio_transacciones/app/servicios/transferencias.py)).
- **Presión controlada sobre el pool.** El pool propio (`PoolConEspera`) limita los
  préstamos con un semáforo y admite una cola corta (`min(10, capacidad)`); si no hay
  slot en `DB_POOL_TIMEOUT_S` (0,05 s por defecto) se rechaza con 503 en lugar de
  encolar indefinidamente ([conexion.py](../servicio_transacciones/app/database/conexion.py)).
- **Tiempos límite en el propio rol de base de datos.** `ddl.sql` fija para
  `svc_banc_user`: `lock_timeout = 800ms`, `statement_timeout = 1500ms` e
  `idle_in_transaction_session_timeout = 5s`. Aplican a toda conexión del servicio,
  independientemente del código de la aplicación.
- **Integridad garantizada en la base.** `saldo >= 0`, `monto > 0`, cuentas distintas,
  divisa con formato de tres letras mayúsculas, unicidad `(transaccion_id, tipo)` en
  movimientos y disparadores que hacen `movimientos` *append-only*, impiden borrar
  transacciones y controlan las transiciones de estado (`COMPLETADA` y `RECHAZADA` son
  finales).

Códigos de respuesta del endpoint **[MVP]**: `201` creada o reproducida, `404` cuenta
inexistente, `409` conflicto de idempotencia, `422` validación, `503` saturación
(pool agotado, deadlock o timeout tras reintentos).

---

## 1.4 Flujo asíncrono

Después del `COMMIT`, la transferencia ya es definitiva para el cliente. El resto del
procesamiento avanza de forma independiente **[MVP]**:

1. **worker_outbox** ejecuta un bucle de sondeo: abre una conexión, reclama un lote de
   hasta 20 eventos `PENDIENTE` y, si no había nada, duerme 1 segundo
   ([main.py](../worker_outbox/app/main.py)).
2. El reclamo usa `FOR UPDATE SKIP LOCKED` y marca los eventos como `PROCESANDO` en una
   transacción corta, de modo que varias instancias del worker podrían repartirse el
   trabajo sin tomar los mismos eventos
   ([repositorios/outbox.py](../worker_outbox/app/repositorios/outbox.py)).
3. Cada evento `TRANSFERENCIA_COMPLETADA` se publica en la cola `cola_ia` de RabbitMQ:
   cola durable, mensajes persistentes (`delivery_mode=2`), `confirm_delivery()` y
   `mandatory=True` ([publicador.py](../worker_outbox/app/publicador.py)).
4. Publicado el mensaje, el evento pasa a `PUBLICADO`. Si la publicación falla, el evento
   vuelve a `PENDIENTE` con `intentos + 1`, y al alcanzar `MAX_INTENTOS_OUTBOX` (3) queda
   en `FALLIDO` ([servicios/outbox.py](../worker_outbox/app/servicios/outbox.py)).
5. **consumidor_ia** consume con `prefetch_count=1` y `auto_ack=False`, calcula el
   análisis, lo persiste en `analisis_ia` y confirma el mensaje (`basic_ack`). Ante un
   error hace `basic_nack(requeue=False)`
   ([consumidor.py](../servicio_ia/app/consumidor.py)).
6. La interfaz consulta el resultado con `GET /demo/analisis/{id}`, que a su vez llama a
   `servicio_ia`. Mientras el análisis no exista, la respuesta es `PENDIENTE` y no un
   error: la asincronía es visible y esperada por la interfaz.

El contexto de traza viaja con el evento: `servicio_transacciones` inyecta el contexto
W3C dentro de `carga_util.trace_context`, el worker lo extrae como padre del span de
publicación y lo reinyecta en el mensaje, y el consumidor lo vuelve a extraer **[MVP]**.
Así una transferencia y su análisis de IA aparecen en la misma traza de Jaeger.

---

## 1.5 Transactional Outbox

El problema que resuelve es la doble escritura: si el servicio confirmara la transferencia
en la base y luego publicara en RabbitMQ, un fallo entre ambos pasos dejaría una
transferencia sin evento (o un evento sin transferencia).

Implementación **[MVP]**:

- La tabla `eventos_outbox` ([ddl.sql](../base_datos/ddl.sql)) guarda `transaccion_id`,
  `tipo_evento`, `carga_util` (`jsonb`), `estado`, `intentos`, `creado_en` y
  `publicado_en`, con clave foránea hacia `transacciones`.
- Los estados son `PENDIENTE`, `PROCESANDO`, `PUBLICADO` y `FALLIDO` (tipo enumerado
  `estados_outbox`).
- El `INSERT` del evento ocurre **dentro de la misma transacción** que mueve los saldos
  ([transferencias.py](../servicio_transacciones/app/servicios/transferencias.py)). Si la
  transferencia se revierte, el evento desaparece con ella.
- Existe un índice parcial sobre `eventos_outbox(creado_en)` restringido a
  `estado = 'PENDIENTE'`, pensado exactamente para la consulta del worker.
- La publicación es **at-least-once**: un fallo después de publicar y antes de marcar
  `PUBLICADO` provoca un reintento y, por tanto, un mensaje duplicado.

Consecuencia de diseño: el consumidor de IA no deduplica por identificador de mensaje.
Lo que absorbe los duplicados es la persistencia: `guardar_analisis` hace
`INSERT ... ON CONFLICT (transaccion_id) DO UPDATE`, y como el análisis es determinista,
reprocesar el mismo evento reescribe el mismo resultado **[MVP]**. Aun así, el trabajo se
repite y el efecto solo es seguro mientras el análisis siga siendo determinista **[LIM]**.
**[PROD]** correspondería deduplicar explícitamente por `id_evento` en el consumidor.

Otra consecuencia **[LIM]**: solo se emite un tipo de evento
(`TRANSFERENCIA_COMPLETADA`). Las transferencias rechazadas no generan evento outbox.

---

## 1.6 Comunicación entre componentes

| Origen | Destino | Tipo | Mecanismo |
|---|---|---|---|
| Navegador | `demo_web` | Síncrono | HTTP/JSON |
| `demo_web` | `servicio_transacciones` | Síncrono | `GET /cuentas`, `GET /cuentas/{numero}/resolver`, `POST /transacciones` |
| `demo_web` | `servicio_ia` | Síncrono | `GET /analisis/{id}/admin` |
| `servicio_transacciones` | PostgreSQL | Síncrono | psycopg2, pool propio por worker |
| `servicio_transacciones` | `worker_outbox` | Asíncrono | tabla `eventos_outbox` (sin llamada directa) |
| `worker_outbox` | RabbitMQ | Asíncrono | `pika`, cola `cola_ia` durable |
| RabbitMQ | `consumidor_ia` | Asíncrono | consumo con ack manual |
| `consumidor_ia` | PostgreSQL | Síncrono | conexión nueva por mensaje |

Separación de identificadores **[MVP]**: los UUID de cuenta solo circulan entre
`demo_web` y `servicio_transacciones`. El navegador trabaja siempre con
`numero_cuenta`, y `demo_web` filtra `cuenta_origen_id`, `cuenta_destino_id` e
`id_idempotencia` de la respuesta antes de devolverla.

---

## 1.7 Observabilidad transversal

Resumen a nivel de arquitectura; el detalle está en
[05_observabilidad.md](05_observabilidad.md).

- **Métricas [MVP]:** Prometheus recolecta cada 5 s de tres destinos:
  `servicio_transacciones:8000/metrics`, `worker_outbox:9101` y `consumidor_ia:9102`.
- **Dashboards [MVP]:** Grafana con datasource y dashboard aprovisionados por archivo.
- **Trazas [MVP]:** OpenTelemetry con exportador OTLP gRPC hacia `jaeger:4317`, propagado
  a través del outbox y de RabbitMQ.
- **Logs [MVP]:** logging estructurado en los tres procesos, con `trace_id`,
  `id_transaccion` y tipo de evento.

---

## 1.8 Límites actuales del MVP

Limitaciones verificadas en el código, no supuestos:

1. **Sin autenticación ni autorización.** Ningún servicio valida identidad del llamante;
   todos los endpoints son abiertos dentro de la red de Compose **[LIM]**.
   **[PROD]**: autenticación de servicio a servicio y de cliente.
2. **`servicio_bancs` no está integrado en el flujo funcional.**
   El servicio está desplegado dentro de `smartbanks_network` y puede
   ser alcanzado por nombre desde los demás contenedores. Sin embargo,
   aunque existe el cliente HTTP
   ([cliente_bancs.py](../worker_outbox/app/servicios/cliente_bancs.py)),
   el flujo actual de `worker_outbox` publica los eventos únicamente en
   RabbitMQ y no invoca a `servicio_bancs` **[LIM]**.
3. **Publicación at-least-once sin deduplicación explícita.** Ver 1.5 **[LIM]**.
4. **Mensajes descartados sin DLQ.** El consumidor responde `basic_nack(requeue=False)`
   ante error y no hay *dead letter queue* declarada: el mensaje se pierde, aunque el
   evento quede registrado en `eventos_outbox` **[LIM]**. **[PROD]**: DLQ y política de
   reintentos con backoff.
5. **Worker outbox por sondeo y de instancia única.** El patrón de reclamo
   (`SKIP LOCKED`) admite concurrencia, pero Compose declara una sola instancia y el
   despertar es por sondeo de 1 s, lo que añade latencia de cola **[LIM]**.
   **[PROD]**: escalado horizontal del worker o notificación por `LISTEN/NOTIFY`.
6. **Métricas Prometheus no agregadas entre workers de Uvicorn.** Con
   `UVICORN_WORKERS=5` y sin modo multiproceso configurado, cada respuesta de `/metrics`
   refleja únicamente el worker que atendió ese scrape **[LIM]**; está documentado en
   [OBSERVABILIDAD_RENDIMIENTO.md](../servicio_transacciones/OBSERVABILIDAD_RENDIMIENTO.md).
7. **`servicio_ia` (API), `servicio_bancs` y `demo_web` no exponen métricas** ni están en
   los *scrape configs* de Prometheus **[LIM]**.
8. **Sin herramienta de migraciones.** El esquema se aplica únicamente en la
   inicialización del contenedor de PostgreSQL; no hay versionado incremental **[LIM]**.
   **[PROD]**: migraciones versionadas.
9. **Conexiones sin pool fuera del servicio de transacciones.** `worker_outbox` abre y
   cierra una conexión por ciclo de bucle y `consumidor_ia` una por mensaje **[LIM]**.
10. **Reintentos del outbox sin backoff.** Un evento que falla vuelve a `PENDIENTE` y
    puede reclamarse en el siguiente ciclo, sin espera creciente **[LIM]**.
11. **El modelo de IA es determinista y simulado** (`smartbancs-mock-v1`): reglas sobre
    monto, tipo y divisa. No hay modelo entrenado **[LIM]** (detalle en
    [04_inteligencia_artificial.md](04_inteligencia_artificial.md)).
12. **Código muerto en el servicio de transacciones.** `transferencias.py` conserva una
    implementación asíncrona previa (`transferir`, `_intento`, `_reproducir`) que no se
    invoca y referencia nombres inexistentes en el módulo (`SessionLocal`, `DBAPIError`,
    `PoolAgotado`); el ajuste `presupuesto_transferencia_s` solo se usa en ese camino
    inactivo. También hay definiciones duplicadas de `CuentaNoEncontrada`,
    `ConflictoIdempotencia` y `validar_idempotencia` **[LIM]**.
13. **Un solo nodo por componente.** No hay réplicas, ni alta disponibilidad de
    PostgreSQL o RabbitMQ; los datos persisten en volúmenes locales de Docker **[LIM]**.
