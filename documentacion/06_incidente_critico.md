# 6. Incidente crítico simulado

Este documento describe cómo actuaría el equipo ante el escenario planteado en el reto.
El análisis posterior y la plantilla de post mortem están en
[07_postmortem.md](07_postmortem.md).

Etiquetas: **[MVP]** implementado y verificable en el código · **[PROD]** propuesta no
implementada · **[LIM]** limitación actual.

> **Nota sobre el alcance.** El escenario es **simulado**. Este documento describe el
> procedimiento y las señales que se usarían; **no afirma que el incidente haya ocurrido**
> ni presenta una causa raíz real. Los ejemplos de causa aparecen marcados como hipótesis.

---

## 6.1 Escenario

Durante un pico transaccional de quincena:

- los usuarios reportan transferencias que no se completan;
- la latencia aumenta de forma severa;
- aparecen múltiples errores de timeout con la base de datos;
- se sospechan deadlocks en las tablas principales.

---

## 6.2 Principio de actuación

El orden de prioridades no se negocia durante el incidente:

1. **Proteger la integridad financiera.** Ningún saldo se corrige a mano, ningún estado se
   edita directamente en la base de datos.
2. **Evitar el doble procesamiento.** Toda acción debe ser compatible con la idempotencia
   ya implementada.
3. **Reducir el impacto al usuario**, aunque sea degradando funcionalidad no esencial.
4. **Recuperar la estabilidad.**
5. **Recopilar evidencia** mientras el incidente está vivo: después ya no estará.
6. **Aplicar correcciones permanentes después**, no durante.

Corolario: en un sistema financiero es preferible **rechazar rápido y de forma limpia**
que aceptar trabajo que no se podrá completar. El MVP ya está construido con ese criterio
(503 con `Retry-After` en lugar de encolar indefinidamente).

---

## 6.3 Detección

Señales disponibles hoy, en el orden en que aparecerían **[MVP]**. El detalle de cada
métrica está en [05_observabilidad.md](05_observabilidad.md); aquí interesa su uso
operativo.

| Orden | Señal | Métrica o fuente | Qué significa |
|---|---|---|---|
| 1 | Espera por conexión creciente | `smartbancs_db_pool_espera_seconds`, `smartbancs_db_pool_semaforo_wait_seconds` | Síntoma **temprano**: aún no hay errores |
| 2 | Tareas en cola del threadpool | `smartbancs_threadpool_tasks_waiting`, `smartbancs_threadpool_queue_seconds` | Llegan más peticiones de las que se procesan |
| 3 | Espera por bloqueos | `smartbancs_db_lock_wait_seconds` | Contención sobre las mismas cuentas |
| 4 | P95 de latencia | panel "Tiempo de respuesta" (`smartbancs_transferencia_duracion_seconds`) | Degradación ya percibida por el usuario |
| 5 | Pool agotado | `smartbancs_db_pool_agotado_total`, `..._rechazos_total{motivo}` | Cada incremento es un **503** devuelto |
| 6 | Timeouts de BD | `smartbancs_db_timeouts_total` | `55P03` (lock) o `57014` (statement) |
| 7 | Deadlocks | `smartbancs_db_deadlocks_total`, panel "Bloqueos de base de datos" | PostgreSQL abortó transacciones |
| 8 | Errores HTTP | `smartbancs_http_errores_total{status,tipo,motivo}` | Qué devuelve el endpoint y por qué |
| 9 | Tasa de error | panel "Porcentaje de errores" | Magnitud del impacto |
| 10 | Reintentos internos | `smartbancs_db_reintentos_total{sqlstate}` | El servicio ya está reintentando por conflicto |

**[LIM] crítico para la detección:** no existen alertas automatizadas. No hay
Alertmanager, ni `rule_files` en Prometheus, ni alertas de Grafana. En el MVP el incidente
se detecta **porque alguien mira el dashboard o porque los usuarios reportan**. Ese es el
primer problema que el post mortem debería registrar.

**[PROD]:** las alertas propuestas en [05_observabilidad.md](05_observabilidad.md#520-alertas)
habrían disparado sobre las señales 1, 5, 6 y 7 antes de que el impacto fuera visible.

---

## 6.4 Primeros minutos: evaluación

Secuencia de comprobación, pensada para descartar rápido antes que para acertar rápido:

1. **Confirmar el alcance.** ¿Fallan todas las transferencias o un subconjunto? El panel
   "Transacciones por segundo" frente a "Transacciones completadas" separa "no llega
   tráfico" de "llega y falla".
2. **Identificar el inicio.** Acotar el minuto en que empieza la desviación y observar su
   forma: un **escalón** sugiere un cambio (despliegue, configuración, un cliente nuevo);
   una **pendiente** sugiere saturación progresiva, que es lo esperable en un pico de
   quincena.
3. **Leer P95 y tasa de error juntos.** Latencia alta con pocos errores es saturación
   incipiente; latencia alta con muchos 503 es saturación ya declarada.
4. **Comprobar disponibilidad de servicios.** `docker compose ps`, `GET /health` del
   servicio de transacciones y el panel de estado de `demo_web` (`/demo/estado`), que
   verifica API, PostgreSQL, worker, RabbitMQ, consumidor, IA y Bancs **[MVP]**.
5. **Comprobar PostgreSQL**: ¿responde?, ¿cuántas sesiones activas?, ¿hay esperas?
   (6.8).
6. **Comprobar el pool**: `smartbancs_db_pool_prestadas` frente al máximo, y el desglose
   de `smartbancs_db_pool_rechazos_total{motivo}`.
7. **Comprobar RabbitMQ y el outbox**, pero **sin desviar la investigación**: eventos
   `PENDIENTE` acumulados son *consecuencia* de un pico, no causa de que una transferencia
   no se complete.
8. **Decidir si el problema está en el camino síncrono o en el asíncrono.**

---

## 6.5 Priorización del camino crítico

```text
CAMINO CRÍTICO (afecta al usuario ahora)
  Navegador/API → servicio_transacciones → PostgreSQL → COMMIT → respuesta 201

CAMINO ASÍNCRONO (no afecta a la confirmación de la transferencia)
  eventos_outbox → worker_outbox → RabbitMQ → consumidor_ia → analisis_ia
```

En este escenario los síntomas son **del camino crítico**: transferencias que no se
completan, timeouts de base de datos y deadlocks. Por tanto la investigación se concentra
en API, pool, SQL, bloqueos y transacciones.

Razonamiento explícito para no equivocar la prioridad: una caída de RabbitMQ o del
consumidor de IA **no puede** impedir que una transferencia se complete, porque el
`COMMIT` ocurre antes de que nada de eso intervenga. Si las transferencias fallan, la
causa está antes del commit. La IA solo entra en la investigación si compite por recursos
compartidos, y eso tiene una vía concreta: `consumidor_ia` abre conexiones contra **la
misma instancia de PostgreSQL** **[LIM]**.

---

## 6.6 Diagnóstico con Grafana

Paneles reales del dashboard `SmartBancs - Monitoreo` y qué patrón esperar:

| Panel | Patrón esperable bajo saturación |
|---|---|
| Transacciones por segundo | Se aplana o cae, aunque el tráfico entrante suba: el sistema no admite más |
| Tiempo de respuesta / Tiempo de las transacciones (P95) | Sube de forma sostenida |
| Porcentaje de errores | Sube **después** de la latencia |
| Transacciones completadas vs con problemas | Se separan: entra tráfico pero no se confirma |
| Errores encontrados (`sum by (tipo)`) | Indica si domina `timeout`, `deadlock` o `db_error` |
| Esperas de base de datos (`smartbancs_db_timeouts_total`) | Crece si hay `55P03` / `57014` |
| Bloqueos de base de datos (`smartbancs_db_deadlocks_total`) | Crece si hay `40P01` |
| Tiempo de base de datos (P95 de `bloquear_cuentas`) | Sube cuando el camino de clasificación se activa por contención |
| Donde ocurrieron los errores | Reparte el error entre transacciones, outbox, RabbitMQ e IA |

**Métricas sin panel**, que en este incidente hay que consultar directamente en Prometheus
**[LIM]**: `smartbancs_db_pool_agotado_total`, `..._rechazos_total{motivo}`,
`smartbancs_db_pool_prestadas`, `smartbancs_db_pool_espera_seconds`,
`smartbancs_threadpool_*`, `smartbancs_db_lock_wait_seconds`,
`smartbancs_db_sql_duracion_seconds{operacion}` y `smartbancs_http_errores_total`. Son
precisamente las que distinguen las causas entre sí, así que la ausencia de panel es una
carencia operativa que el post mortem debe recoger.

**Cuidado al leer los contadores acumulados**: con 5 workers de Uvicorn y sin modo
multiproceso, cada scrape refleja un solo proceso; los valores absolutos fluctúan. Durante
un incidente conviene mirar `rate(...)` y tendencias, no totales
([05_observabilidad.md](05_observabilidad.md)).

---

## 6.7 Diagnóstico con logs y trazas

**Logs.** Los del servicio de transacciones son JSON con `timestamp`, `evento`,
`trace_id`, `id_transaccion`, `sqlstate`, `operacion`, `motivo` y `tipo_error` **[MVP]**.
Procedimiento:

1. Seleccionar un **error representativo** de la ventana afectada, no el primero que
   aparezca: conviene uno del periodo de mayor densidad.
2. Tomar su `id_transaccion` (correlación de negocio) y su `trace_id`.
3. Localizar los eventos relacionados: `db_timeout`, `db_deadlock` o `db_error`, con su
   `sqlstate` y su `operacion`.
4. Identificar el **último punto exitoso** antes del fallo.
5. Contar la distribución de `sqlstate` en la ventana: es el dato que más rápido separa
   hipótesis. `55P03` apunta a contención por bloqueos; `57014` a sentencias que no
   terminan a tiempo; `40P01` a deadlock confirmado.

Recordatorio al correlacionar: el campo `trace_id` **no significa lo mismo en todos los
servicios** (el del servicio transaccional es un UUID de aplicación; el del consumidor de
IA es el trace ID de OpenTelemetry). Para cruzar componentes, el identificador fiable es
`id_transaccion` **[LIM]**.

**Trazas.** En Jaeger (`http://localhost:18086`), buscar por servicio
`servicio_transacciones` y **ordenar por duración**. En una traza lenta:

| Observación | Interpretación |
|---|---|
| `db.actualizar_saldos` domina la duración | El `UPDATE` de saldos espera por bloqueos de fila |
| Aparece `db.clasificar_rechazo` y `db.bloquear_cuentas` con `db.lock_wait_ms` alto | Se activó el camino de rechazo y hay contención real |
| `idempotencia.obtener_o_crear` lento | La lectura o el `INSERT` inicial se está degradando: apunta a la tabla `transacciones`, no a `cuentas` |
| Hueco entre el inicio del span HTTP y el primer span de BD | El tiempo se va **antes** de tocar la base: threadpool o espera de conexión del pool |
| `outbox.insertar_evento` lento | Contención en la tabla de eventos |

Ese último caso —el hueco previo— es el que distingue "la base de datos está lenta" de
"no conseguimos siquiera una conexión". Es una distinción central en este incidente.

---

## 6.8 Diagnóstico directo en PostgreSQL

Consultas de **solo lectura**, seguras durante un incidente **[MVP, manual]**. No hay
exporter de PostgreSQL, así que esto se ejecuta a mano
(`docker exec -it smartbanks_database psql -U ... -d ...`).

### `pg_stat_activity` — qué está ocurriendo ahora

```sql
SELECT pid, application_name, state,
       now() - xact_start  AS duracion_transaccion,
       now() - query_start AS duracion_query,
       wait_event_type, wait_event,
       left(query, 120)    AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
ORDER BY xact_start;
```

Qué leer:

- **Número de sesiones** y su reparto por `application_name` (`smartbancs`,
  `worker_outbox`, `consumidor_ia`): confirma quién está consumiendo conexiones.
- **`state`**: muchas en `active` con `duracion_query` alta indican trabajo lento;
  `idle in transaction` indica transacciones abiertas sin avanzar, que es lo más dañino
  porque retienen bloqueos. El rol tiene `idle_in_transaction_session_timeout = 5s`, así
  que una sesión así no debería sobrevivir mucho **[MVP]**.
- **`wait_event_type` / `wait_event`**: `Lock` apunta a contención entre transacciones;
  `IO` a disco; `LWLock` a contención interna del servidor. Son diagnósticos distintos.
- **Duración de transacción frente a duración de consulta**: una transacción larga con
  consultas cortas señala una sesión que abrió y no cerró.

### `pg_locks` — quién bloquea a quién

```sql
SELECT bloqueada.pid        AS pid_bloqueada,
       bloqueada.query      AS query_bloqueada,
       bloqueante.pid       AS pid_bloqueante,
       bloqueante.query     AS query_bloqueante,
       bloqueante.state     AS estado_bloqueante,
       now() - bloqueada.query_start AS espera
FROM pg_stat_activity AS bloqueada
JOIN LATERAL unnest(pg_blocking_pids(bloqueada.pid)) AS b(pid) ON true
JOIN pg_stat_activity AS bloqueante ON bloqueante.pid = b.pid
WHERE cardinality(pg_blocking_pids(bloqueada.pid)) > 0;
```

Esto responde: **quién bloquea**, **a cuántos**, **desde cuándo** y **con qué consulta**.
Para el detalle del tipo de bloqueo y la relación afectada se consulta `pg_locks` con
`relation::regclass`, `mode` y `granted`.

**Detectar un bloqueo no autoriza a terminar la sesión.** Antes hay que establecer: quién
bloquea, cuánto tiempo lleva, qué está ejecutando y qué impacto real produce. Una sesión
bloqueante que está a punto de confirmar su trabajo no debe tocarse: terminarla convierte
un retraso en una transacción perdida. El procedimiento está en 6.11.

### `pg_stat_statements` — qué consume el tiempo acumulado

```sql
SELECT calls, total_exec_time, mean_exec_time, rows, left(query, 120) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

Es una visión **acumulada**, no instantánea: sirve para encontrar la sentencia que domina
la carga por **tiempo total** (frecuencia × coste), que a menudo no es la más lenta
individualmente. Ordenando por `mean_exec_time` se encuentra la más cara por ejecución.

Como el SQL de SmartBancs está escrito a mano y es estable, las sentencias se agrupan de
forma limpia y este contraste es fiable: permite comparar el coste real del `UPDATE` de
saldos, del `INSERT` de la transacción y del `SELECT` de idempotencia.

**Log del servidor** como fuente independiente de la aplicación: `log_lock_waits=on`
registra las esperas de bloqueo largas y `log_min_duration_statement=250ms` las sentencias
lentas; el `log_line_prefix` incluye `application_name`, así que se puede atribuir cada
línea a su componente **[MVP]**.

---

## 6.9 Distinguir las causas entre sí

El error visible —503 o timeout— es el mismo para varias causas distintas. Esta tabla es
la que evita diagnosticar por intuición:

| Causa | Señal que la confirma | Señal que la descarta |
|---|---|---|
| **Pool agotado** (falta de conexiones) | `smartbancs_db_pool_rechazos_total{motivo="timeout"\|"cola_llena"}` y `..._agotado_total` crecen; `..._prestadas` en el máximo; en Jaeger, hueco **antes** del primer span de BD | Las consultas que sí se ejecutan tardan lo normal |
| **Base de datos lenta** | `smartbancs_db_sql_duracion_seconds{operacion}` sube; `pg_stat_statements` muestra tiempos medios altos; aparecen `57014` | El pool no rechaza y `..._prestadas` no está en el máximo |
| **Contención de bloqueos** | `smartbancs_db_lock_wait_seconds` sube; `55P03`; `wait_event_type = 'Lock'`; `pg_blocking_pids` devuelve resultados | `pg_stat_activity` no muestra esperas por `Lock` |
| **Deadlock** | `smartbancs_db_deadlocks_total` crece; `40P01` en los logs; el servidor lo registra | Ausencia de `40P01` |
| **Conexión perdida / red** | SQLSTATE de clase `08*`, `InterfaceError`, conexiones descartadas al devolverlas al pool | Las sesiones existentes funcionan con normalidad |
| **Saturación de CPU/memoria** | **No medible hoy** **[LIM]**: no hay métricas de contenedor. Solo por `docker stats` manual | — |
| **Carga simplemente mayor que la capacidad** | Throughput plano en su techo con latencia creciente y sin errores de BD específicos | Alguna de las anteriores presenta señal clara |

Hay que resistir la tentación de detenerse en la primera coincidencia: en un pico real
suelen darse **varias a la vez**, y lo que importa es cuál es causa y cuáles son
consecuencia. La contención de bloqueos, por ejemplo, alarga las transacciones, lo que
retiene conexiones más tiempo, lo que agota el pool: tres síntomas, un solo origen.

---

## 6.10 Acciones inmediatas, clasificadas por riesgo

### Riesgo bajo — observación y contención

| Acción | Estado | Notas |
|---|---|---|
| Registrar la hora de inicio y capturar evidencia (consultas de 6.8, capturas de paneles, logs de la ventana) | Operativa **[MVP]** | Debe hacerse **antes** de mitigar: mitigar borra la evidencia |
| Dejar que el mecanismo de protección actúe | Automático **[MVP]** | El pool rechaza con 503 + `Retry-After: 1` en lugar de encolar; los reintentos internos están acotados a 3 |
| **Pausar el consumidor de IA** (`docker compose stop consumidor_ia`) | Operativa **[MVP]** | Libera conexiones de PostgreSQL. **Seguro**: los mensajes permanecen en la cola durable y se procesan al reanudar |
| **Pausar el worker de outbox** (`docker compose stop worker_outbox`) | Operativa **[MVP]** | Libera una conexión y carga de sondeo. **Seguro**: los eventos quedan `PENDIENTE` en la tabla y se publican al reanudar |
| Pausar cargas no esenciales (ETL, pruebas de carga) | Operativa **[MVP]** | El ETL se ejecuta a mano; basta con no lanzarlo |
| Revisar el dimensionamiento del pool **sin cambiarlo todavía** | Operativa | Ver 6.13 |

Las dos pausas son la mitigación más valiosa del MVP y merecen explicación: `consumidor_ia`
abre **una conexión nueva por mensaje** y `worker_outbox` **una por ciclo de sondeo**,
ambas contra la misma instancia que atiende las transferencias. Detenerlos devuelve
capacidad al camino crítico **sin perder un solo dato**, porque el diseño asíncrono ya
garantiza que nada se pierde mientras están parados. Es exactamente el tipo de degradación
controlada que el sistema fue diseñado para permitir.

### Riesgo medio — requiere decisión y medición

| Acción | Estado | Condición previa |
|---|---|---|
| Limitar temporalmente el tráfico entrante (*rate limiting*) | **[PROD]**, no implementado | Requiere un proxy inverso o pasarela delante del servicio; hoy no existe (6.12) |
| Ajustar `UVICORN_WORKERS` o el tamaño del pool | Operativa, implica reinicio | Solo con evidencia de cuál es el límite; ver 6.13 y 6.14 |
| Cancelar una consulta concreta (`pg_cancel_backend`) | Operativa **[MVP]** | Solo tras identificar la sesión y su impacto (6.11) |
| Escalar horizontalmente el servicio | **[LIM]**, ver 6.14 | No es posible con `docker compose --scale` mientras los servicios declaren `container_name` fijo |

### Riesgo alto — último recurso

| Acción | Condición |
|---|---|
| Terminar un backend de PostgreSQL (`pg_terminate_backend`) | Solo tras agotar la cancelación y confirmar impacto (6.11) |
| Reiniciar `servicio_transacciones` | Aborta las transferencias en curso; el cliente puede reintentar con la misma clave de idempotencia, pero es una interrupción |
| Reiniciar PostgreSQL | Última opción. Aborta **todas** las transacciones en vuelo |

### Lo que no se hace nunca

- **Modificar saldos, estados o movimientos a mano** para "cuadrar" el incidente. Los
  triggers del esquema lo impiden en parte (`movimientos` es *append-only*, los estados
  finales no se revierten, el rol no tiene `DELETE`), y saltárselos con un superusuario
  destruiría la integridad y la auditoría.
- **Desactivar la idempotencia o los timeouts** para "dejar pasar" tráfico: es lo que
  convierte una degradación en un problema de dinero.
- **Terminar conexiones de forma indiscriminada.**

---

## 6.11 Finalizar sesiones bloqueantes: procedimiento

El reto menciona "finalizar conexiones bloqueadas" como ejemplo de acción inmediata. Es
una acción **legítima pero de último recurso**, y el orden importa.

1. **Identificar la sesión bloqueante** con la consulta de `pg_blocking_pids` (6.8). No la
   bloqueada: la que bloquea.
2. **Confirmar que causa impacto real**: ¿a cuántas sesiones bloquea?, ¿desde hace cuánto?
   Una espera de 300 ms en un pico no es un incidente; una de 30 segundos sí.
3. **Determinar qué está haciendo**: `state = 'active'` con una consulta en curso no es lo
   mismo que `idle in transaction`. Una sesión activa puede estar a punto de terminar su
   trabajo; una inactiva con transacción abierta solo retiene bloqueos.
4. **Cancelar la consulta primero**: `pg_cancel_backend(pid)` interrumpe la sentencia en
   curso y deja la sesión viva. Es la opción reversible.
5. **Terminar el backend solo si la cancelación no surte efecto**:
   `pg_terminate_backend(pid)` cierra la conexión completa.
6. **Validar el resultado**: comprobar que la espera se liberó y que la transacción
   afectada quedó revertida, no a medias.

### Por qué esto es seguro para el dinero

PostgreSQL garantiza atomicidad: si una sesión se cancela o se termina **antes del
`COMMIT`**, su transacción se revierte **entera**. No quedan saldos a medias ni un débito
sin su crédito, porque todo ocurre dentro de la misma transacción
([02_backend_base_datos.md](02_backend_base_datos.md)).

Además, la aplicación colabora con ese modelo **[MVP]**:

- `57014` (consulta cancelada) es uno de los SQLSTATE que el servicio trata como
  reintentable, así que una cancelación puede incluso resolverse sola en el siguiente
  intento;
- la conexión afectada se **descarta** al devolverse al pool si quedó inutilizable, en
  lugar de reutilizarse sucia;
- el cliente recibe un 503 con instrucción explícita de reintentar **con la misma clave de
  idempotencia**, y ese reintento no puede duplicar la transferencia.

El peor resultado de cancelar una sesión es una transferencia que no se completó y debe
reintentarse. El peor resultado de **no** cancelarla puede ser el bloqueo prolongado de
todo el sistema. Esa es la comparación que hay que hacer, con datos, antes de actuar.

---

## 6.12 Limitación temporal del tráfico

**No está implementada** **[LIM]**. No hay *rate limiting*, ni límite de peticiones por
cliente, ni proxy inverso delante del servicio.

Sería útil exactamente en este escenario: cuando entra más trabajo del que el sistema
puede completar, admitir todo garantiza que **nada** termine a tiempo. Limitar la entrada
permite que una parte del tráfico se complete con normalidad mientras el resto recibe un
rechazo rápido y reintentable, en lugar de una degradación generalizada.

Conviene notar que el MVP **ya aplica ese principio hacia dentro**: el pool con cola corta
y rechazo a los 50 ms es, en la práctica, un limitador de admisión a nivel de conexión
**[MVP]**. Lo que falta es el equivalente en el borde, por cliente.

**[PROD]:** limitación en la pasarela o proxy inverso, por cliente y por endpoint, con
respuesta 429 y `Retry-After`, coherente con el 503 que ya devuelve el servicio.

---

## 6.13 Ajuste de concurrencia y del pool

**Más workers no es automáticamente mejor.** Aumentar `UVICORN_WORKERS` multiplica las
conexiones a PostgreSQL (cada worker tiene su propio pool), aumenta la competencia por CPU
y puede **incrementar la contención de bloqueos** sobre las mismas filas. Si el cuello de
botella está en la base de datos, más workers empeoran el incidente.

**Aumentar el pool tampoco lo es.** La restricción que debe cumplirse siempre:

```text
workers × (pool_size + max_overflow)  +  conexiones de worker_outbox y consumidor_ia
                                      <  max_connections  (con margen)
```

Valores actuales verificados **[MVP]** ([docker-compose.yml](../docker-compose.yml)):

| Parámetro | Valor |
|---|---|
| `UVICORN_WORKERS` | 5 |
| `DB_POOL_SIZE` | 15 |
| `DB_MAX_OVERFLOW` | 0 |
| `DB_POOL_TIMEOUT_S` | 0.05 |
| `ANYIO_THREAD_TOKENS` | 40 |
| Conexiones del servicio transaccional | 5 × 15 = **75** |
| `max_connections` de PostgreSQL | **200** |

Queda margen nominal, pero **margen de conexiones no es margen de capacidad**: si las
transacciones se alargan por contención, más conexiones simultáneas solo aumentan la
competencia por las mismas filas. Por eso el ajuste debe hacerse **después** de determinar
la causa (6.9), y midiendo el efecto, no antes.

Un desequilibrio ya visible en la configuración: el threadpool admite 40 tareas síncronas
por worker, pero solo 15 pueden sostener una conexión. Es deliberado —el exceso se rechaza
rápido en lugar de encolarse— pero significa que bajo carga alta el 503 por pool llega
antes que cualquier otra señal.

---

## 6.14 Escalamiento horizontal

**Ayudaría si** el cuello de botella es CPU de la API o capacidad de proceso, y el tráfico
es distribuible.

**No resolvería nada si** el cuello está en PostgreSQL: más instancias de API contra la
misma base añaden conexiones y contención sobre las mismas cuentas. Con una consulta lenta
o con bloqueos sobre filas concretas, escalar **empeora** la situación.

**[LIM] del MVP:** hay una sola instancia de cada servicio y todos declaran
`container_name` fijo en `docker-compose.yml`, lo que **impide** usar
`docker compose up --scale`. El escalado real exigiría quitar esos nombres fijos y poner un
balanceador delante. Solo `worker_outbox` está preparado a nivel de diseño para varias
instancias, gracias al `FOR UPDATE SKIP LOCKED` de su reclamo de eventos.

---

## 6.15 Configuración de red

El reto menciona ajustar la configuración de red como posible acción. **Primero debe
existir evidencia**: pérdida de paquetes, latencia anómala entre contenedores, timeouts de
conexión TCP (no de consulta) o errores con SQLSTATE de clase `08*`.

En el MVP, todos los servicios comparten una red bridge de Docker en el mismo host, por lo
que la red es el sospechoso **menos probable** de este escenario. Los síntomas descritos
—timeouts de base de datos y deadlocks— apuntan a contención, no a transporte.

Cambiar parámetros de red sin diagnóstico añade una variable nueva en mitad de un
incidente, que es justo lo contrario de lo que se necesita.

---

## 6.16 Recuperación

Una vez contenida la causa, la vuelta a la normalidad es **gradual y verificada**:

1. **Confirmar que PostgreSQL responde con normalidad**: sesiones activas en un número
   razonable, sin esperas por `Lock` prolongadas, sin sesiones `idle in transaction`.
2. **Confirmar que el pool se recuperó**: `smartbancs_db_pool_prestadas` por debajo del
   máximo y sin nuevos incrementos en `smartbancs_db_pool_rechazos_total`.
3. **Revisar la tasa de error**: debe volver a su línea base, no simplemente "bajar".
4. **Verificar el P95** frente al valor previo al incidente.
5. **Reanudar el tráfico de forma gradual** si se limitó, observando entre pasos.
6. **Reanudar los procesos asíncronos** que se pausaron:
   `docker compose start worker_outbox consumidor_ia`. Hacerlo **después** de que el
   camino crítico esté estable, no antes: al arrancar procesarán el atraso acumulado y
   añadirán carga a la base de datos.
7. **Vigilar el drenaje del backlog**: `smartbancs_outbox_eventos_total{resultado}` y
   `smartbancs_ia_mensajes_total{resultado="procesado"}` deben crecer, y la cantidad de
   eventos `PENDIENTE` en la tabla debe bajar de forma sostenida.
8. **Comprobar los eventos `FALLIDO`**, que son los que agotaron sus tres intentos y
   requieren decisión aparte.
9. **Verificar la IA** al final: es lo último que importa y lo único que puede esperar.

El orden no es cosmético: reanudar los consumidores antes de estabilizar el camino crítico
reintroduce exactamente la presión que se acababa de retirar.

---

## 6.17 Validación funcional

Los dashboards dicen que el sistema responde; no dicen que responda **bien**. Hace falta
una prueba dirigida **[MVP]**:

1. Ejecutar **una transferencia controlada** entre dos cuentas de prueba conocidas,
   anotando la clave de idempotencia.
2. Verificar que la respuesta es `201` con `estado = "COMPLETADA"`.
3. Verificar en base de datos:
   - el **saldo** de origen y destino cambió exactamente en el importe;
   - existen **dos movimientos** (`DEBITO` y `CREDITO`) para esa transacción, con su
     `saldo_resultante`;
   - la transacción quedó `COMPLETADA` con `completado_en` informado;
   - existe **un evento** en `eventos_outbox` para esa transacción.
4. **Repetir la misma petición con la misma clave de idempotencia** y comprobar que
   devuelve `reproducida = true`, que **no** se crean movimientos nuevos y que los saldos
   no vuelven a moverse. Esta es la comprobación que confirma que los reintentos del
   incidente no duplicaron dinero.
5. Confirmar que el análisis de IA aparece al cabo de unos segundos (o que su ausencia se
   explica por el backlog pendiente).

---

## 6.18 Validación de integridad

Comprobaciones de solo lectura sobre el esquema real **[MVP]**. Todas deberían devolver
cero filas; cualquier resultado exige investigación, **no corrección manual**.

**Transferencias completadas sin sus dos movimientos:**

```sql
SELECT t.id_transaccion, count(m.id_movimiento) AS movimientos
FROM transacciones t
LEFT JOIN movimientos m ON m.transaccion_id = t.id_transaccion
WHERE t.estado = 'COMPLETADA'
GROUP BY t.id_transaccion
HAVING count(m.id_movimiento) <> 2;
```

**Transferencias completadas sin evento de salida:**

```sql
SELECT t.id_transaccion
FROM transacciones t
LEFT JOIN eventos_outbox e ON e.transaccion_id = t.id_transaccion
WHERE t.estado = 'COMPLETADA' AND e.id_eventos IS NULL;
```

**Transacciones que quedaron `PENDIENTE`:** no deberían existir tras un commit, porque el
estado se resuelve dentro de la misma transacción.

```sql
SELECT id_transaccion, creado_en
FROM transacciones
WHERE estado = 'PENDIENTE'
ORDER BY creado_en;
```

**Importes de los movimientos que no cuadran con su transacción:**

```sql
SELECT m.transaccion_id
FROM movimientos m
JOIN transacciones t ON t.id_transaccion = m.transaccion_id
GROUP BY m.transaccion_id, t.monto
HAVING min(m.monto) <> t.monto OR max(m.monto) <> t.monto;
```

**Trabajo asíncrono pendiente o fallido:**

```sql
SELECT estado, count(*), min(creado_en) AS mas_antiguo
FROM eventos_outbox
WHERE estado <> 'PUBLICADO'
GROUP BY estado;
```

**Eventos atascados en `PROCESANDO`** (un worker que murió entre reclamar y publicar):

```sql
SELECT id_eventos, transaccion_id, intentos, creado_en
FROM eventos_outbox
WHERE estado = 'PROCESANDO'
  AND creado_en < now() - interval '5 minutes';
```

Esto último es un hueco real del diseño: un evento reclamado por un worker que muere queda
en `PROCESANDO` y **ningún mecanismo lo devuelve a `PENDIENTE`** **[LIM]**.

**Transferencias sin análisis de IA:**

```sql
SELECT count(*)
FROM transacciones t
LEFT JOIN analisis_ia a ON a.transaccion_id = t.id_transaccion
WHERE t.estado = 'COMPLETADA' AND a.id_analisi IS NULL;
```

Un resultado alto justo después del incidente es normal (backlog). Si no baja al reanudar
los consumidores, apunta a mensajes descartados con `basic_nack(requeue=False)`, que **no
se reintentan nunca** **[LIM]**.

**Saldos negativos**: la restricción `CHECK (saldo >= 0)` los impide, así que esta
comprobación es de confirmación, no de búsqueda.

---

## 6.19 Reconciliación posterior

Después de estabilizar, y **como proceso controlado**, no como edición manual:

| Qué revisar | Cómo | Decisión |
|---|---|---|
| Transferencias que fallaron con 503 | Logs de la ventana + `smartbancs_http_errores_total` | No requieren acción del sistema: el cliente reintenta con su clave de idempotencia. Sí requieren comunicación |
| Transacciones `PENDIENTE` | Consulta de 6.18 | Investigar caso a caso; no cambiar el estado a mano |
| Eventos `PENDIENTE` acumulados | Tabla `eventos_outbox` | Se drenan solos al reanudar el worker |
| Eventos `PROCESANDO` antiguos | Consulta de 6.18 | **[LIM]** requieren intervención: no hay recuperación automática |
| Eventos `FALLIDO` | Agotaron 3 intentos | Analizar la causa antes de reprocesar |
| Análisis de IA faltantes | Consulta de 6.18 | Sin impacto financiero; el análisis puede regenerarse invocando el flujo |

Regla general: **la reconciliación diagnostica y propone; no corrige dinero**. Cualquier
corrección que implique saldos debe tener su propia transacción, su propia trazabilidad y
aprobación explícita.

---

## 6.20 Escalamiento y roles

Estructura mínima, sin inventar una organización concreta **[PROD]**: en el MVP no existe
un procedimiento de escalamiento documentado ni guardias definidas **[LIM]**.

| Nivel | Quién | Cuándo se involucra |
|---|---|---|
| **N1** | Operación / desarrollo de guardia | Detección, primera evaluación, acciones de riesgo bajo |
| **N2** | Backend, base de datos, infraestructura | El incidente persiste tras las acciones de N1, o requiere decisiones de riesgo medio |
| **N3** | Arquitectura, DBA, proveedor de infraestructura o responsable del core legado | Acciones de riesgo alto, pérdida de datos potencial, o causa fuera del sistema |

**Roles durante el incidente** (en un equipo pequeño, una persona puede cubrir varios,
pero los roles deben estar nombrados explícitamente):

- **Incident Commander**: coordina, decide y mantiene el foco. **No diagnostica**; su
  trabajo es que el resto pueda hacerlo.
- **Responsable de backend**: logs, trazas, comportamiento del servicio.
- **Responsable de base de datos**: `pg_stat_activity`, `pg_locks`,
  `pg_stat_statements`, decisiones sobre sesiones bloqueantes.
- **Responsable de infraestructura**: recursos, red, contenedores.
- **Comunicación**: informa a usuarios y partes interesadas, y **lleva la bitácora**.

Criterios de escalamiento sugeridos: impacto sobre transferencias que no se completan,
duración superior al umbral acordado sin mejora, o necesidad de una acción de riesgo alto.

---

## 6.21 Comunicación y bitácora

Durante el incidente debe registrarse, con hora:

- **inicio** de la degradación y momento de la detección;
- **síntomas** observados y su evolución;
- **impacto**: qué falla, para quién, en qué volumen;
- **acciones ejecutadas**, quién las ejecutó y **por qué**;
- **resultado** de cada acción (incluidas las que no funcionaron);
- **decisiones** tomadas y descartadas.

Esta bitácora es lo que alimenta el post mortem. Reconstruirla después de memoria produce
una narración ordenada y falsa: se recuerda lo que se acabó entendiendo, no lo que se sabía
en cada momento. Anotar también **lo que no funcionó** es lo que distingue un post mortem
útil de uno ceremonial.

Hacia el exterior: comunicar pronto, con lenguaje claro, qué está afectado y qué debe hacer
el usuario. En este sistema hay un mensaje concreto y accionable: **un reintento con la
misma operación es seguro**, porque la idempotencia impide el doble cargo.

---

## 6.22 Criterios de cierre

Un incidente no se cierra porque "ya funciona". Se cierra cuando, durante una **ventana de
observación suficiente** (acordada por el equipo, no improvisada):

- el P95 de latencia está estable en su valor de referencia;
- la tasa de error volvió a la línea base;
- no hay nuevos incrementos en `smartbancs_db_pool_agotado_total` ni en
  `..._rechazos_total`;
- no hay esperas de bloqueo prolongadas ni nuevos `40P01`;
- una transferencia de validación se completó correctamente (6.17);
- las comprobaciones de integridad (6.18) no arrojan anomalías;
- el backlog asíncrono está drenado o decreciendo de forma sostenida;
- las acciones temporales aplicadas están **documentadas** y tienen fecha de reversión.

Este último punto se olvida con frecuencia: una mitigación temporal que nadie revierte se
convierte en configuración permanente sin que nadie lo haya decidido.

---

## 6.23 Qué mitiga el MVP y qué no

**Mecanismos existentes que reducen el impacto de este escenario [MVP]:**

| Mecanismo | Qué aporta en este incidente |
|---|---|
| **Idempotencia** (`UNIQUE` + hash + savepoint) | Hace que el reintento del cliente sea seguro: la recuperación no duplica dinero |
| **Transacción ACID única** | Ningún fallo deja saldos a medias; cancelar una sesión revierte todo |
| **Timeouts en el rol de PostgreSQL** | Convierten una espera indefinida en un error clasificable y acotan el daño de una sesión atascada |
| **Pool con rechazo rápido** | Evita que la cola de espera crezca sin límite; degrada de forma predecible |
| **Reintentos acotados con backoff y jitter** | Absorben conflictos transitorios sin amplificar el pico |
| **Orden determinista de actualización** | Reduce la probabilidad de deadlock |
| **Transactional Outbox** | Permite pausar el procesamiento asíncrono sin perder eventos |
| **Cola durable con ack manual** | Permite pausar el consumidor sin perder mensajes |
| **Instrumentación por capas** | Permite localizar el escalón donde se va el tiempo |
| **Triggers de inmutabilidad** | Impiden "arreglar" el incidente corrompiendo el libro mayor |

**Lo que el MVP no evita [LIM]:**

- No evita el incidente: si la carga supera la capacidad, habrá degradación.
- No lo **detecta solo**: sin alertas, depende de que alguien esté mirando.
- No se **recupera solo**: no hay autoescalado ni limitación de admisión en el borde.
- No hay **alta disponibilidad**: PostgreSQL es una única instancia sin réplica ni
  failover; su caída es una caída total.
- No hay **runbook** ejecutable ni simulacros previos.
- No hay **métricas de recursos** (CPU, memoria) ni de **profundidad de cola**, que son dos
  de las señales que más ayudarían aquí.
- Los eventos que quedan en `PROCESANDO` por la muerte de un worker **no se recuperan
  automáticamente**.

El detalle de estas carencias, con acciones correctivas priorizadas, está en
[07_postmortem.md](07_postmortem.md).
