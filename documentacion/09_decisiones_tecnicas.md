# 9. Decisiones técnicas

El reto plantea una solución tecnológicamente agnóstica y exige **justificar cada decisión
con base en el rendimiento, la seguridad y la escalabilidad del caso de uso**. Este
documento responde a eso: no enumera tecnologías, explica por qué cada una está donde
está, qué problema concreto de SmartBancs resuelve y qué cuesta.

La arquitectura se describe en [01_arquitectura.md](01_arquitectura.md) y el detalle del
núcleo transaccional en [02_backend_base_datos.md](02_backend_base_datos.md). Aquí no se
repiten: se referencian.

Etiquetas: **[MVP]** implementado y verificable · **[PROD]** propuesta no implementada ·
**[LIM]** limitación actual.

---

## 9.0 El flujo activo, verificado

Antes de justificar nada hay que saber qué código se ejecuta. Flujo real de
`POST /transacciones`, comprobado en el repositorio:

```text
rutas/transferencias.py::crear_transferencia   (def síncrona, no async)
  → servicios/hash_solicitud.py::generar_hash_solicitud   (SHA-256)
  → servicios/transferencias.py::procesar_transferencia   (bucle de reintentos)
      → database/conexion.py::obtener_conexion            (pool propio, psycopg2)
      → servicios/transferencias.py::_procesar_transferencia
          → servicios/idempotencia.py::obtener_o_crear_transaccion  (SAVEPOINT)
          → repositorios/transacciones.py::debitar_cuenta / acreditar_cuenta
          → (solo en rechazo) obtener_cuentas_para_actualizar  → FOR NO KEY UPDATE
          → insertar_movimiento ×2 → completar_transaccion → insertar_evento_outbox
      → COMMIT
```

**Advertencias sobre código que NO está activo [LIM]**, para no justificar decisiones
inexistentes:

- `servicios/transferencias.py` conserva una implementación asíncrona previa (`transferir`,
  `_intento`, `_reproducir`) que **nadie invoca** y que referencia `SessionLocal` y
  `DBAPIError`, nombres no importados en el módulo. **SQLAlchemy ni siquiera está en los
  requirements**, así que ese código fallaría al ejecutarse. No es la arquitectura.
- El ajuste `presupuesto_transferencia_s` solo se usa en ese camino muerto.
- **No se usa `SELECT ... FOR UPDATE` en el camino normal.** El único bloqueo explícito es
  `FOR NO KEY UPDATE`, y solo en la ruta de clasificación del rechazo.
- **El worker de outbox no publica hacia Bancs**: solo hacia RabbitMQ. El cliente HTTP de
  Bancs existe pero nadie lo llama ([03_bancs_etl.md](03_bancs_etl.md)).
- **`consumidor_ia` no llama por HTTP a `servicio_ia`**: importa la función de análisis y
  la ejecuta en su propio proceso ([04_inteligencia_artificial.md](04_inteligencia_artificial.md)).

---

## 9.1 Del problema a la decisión

Cada decisión responde a un problema concreto del caso de uso, no a una preferencia:

| Problema del caso de uso | Decisión principal |
|---|---|
| Integridad financiera absoluta | PostgreSQL + transacción ACID única + constraints y triggers |
| Exactitud monetaria | `Decimal` en Python y `NUMERIC(18,2)` en la base |
| Race conditions y doble gasto | `UPDATE` condicionado sobre fila bloqueada + orden determinista |
| Reintentos del cliente (timeout, doble clic, red) | Clave de idempotencia + hash SHA-256 + `UNIQUE` |
| Transferencia por debajo de 2 segundos | SQL directo, transacción corta, trabajo no crítico fuera del commit |
| Proteger PostgreSQL de la saturación | Pool con cola acotada, rechazo rápido y timeouts en el rol |
| Que la IA no bloquee la transferencia | Transactional Outbox + RabbitMQ + consumidor separado |
| Que un fallo del broker no pierda eventos | Evento escrito en la misma transacción financiera |
| Integración futura con el core legado | Mismo patrón outbox, sin tocar el camino crítico |
| Diagnóstico de degradaciones | Métricas por capas, trazas distribuidas y logs estructurados |
| Reproducibilidad del entorno | Docker Compose con un solo comando |
| Medir capacidad de forma repetible | k6 con tasa de llegada constante y datasets versionados |

---

## 9.2 Lenguaje y framework

### D1. Python **[MVP]**

**Problema.** Construir, en el tiempo de un reto, cuatro servicios HTTP, dos procesos de
fondo, un ETL y una capa de observabilidad completa, sin sacrificar corrección financiera.

**Decisión.** Python 3.12 (imágenes `python:3.12-slim`) en todos los componentes.

**Rendimiento.** No se eligió por velocidad de CPU: Python no es el lenguaje más rápido en
cómputo. La elección se sostiene porque **el trabajo pesado de este sistema no está en el
proceso Python**. El camino crítico es esencialmente E/S: una transferencia son ocho
sentencias SQL dentro de una transacción, y el tiempo se va en la base de datos y en la
red, no en el intérprete. La lógica en Python se reduce a validar, calcular un hash y
orquestar. Donde sí habría penalización —cómputo intensivo— no hay nada.

**Seguridad e integridad.** Pydantic da validación declarativa en el borde; `Decimal` está
en la biblioteca estándar y se usa de forma explícita para montos; psycopg2 parametriza el
SQL. El ecosistema de las piezas críticas (PostgreSQL, RabbitMQ, OpenTelemetry) es maduro y
tiene clientes oficiales.

**Escalabilidad.** El GIL limita el paralelismo **dentro** de un proceso, y por eso el
diseño escala con **procesos**, no con hilos: 5 workers de Uvicorn, procesos de fondo
separados, servicios independientes. Es el patrón correcto para Python.

**Trade-off.** El GIL y el menor rendimiento en tareas CPU-bound frente a Go, Java o Rust.
Si el modelo de IA fuera pesado o hubiera cálculo intensivo en el camino crítico, la
elección merecería revisarse.

**Alternativas.** **Go** (mejor concurrencia nativa y menor consumo por conexión), **Java**
(ecosistema bancario consolidado, JDBC maduro), **Rust** (máximo control). No se
priorizaron porque ninguna resuelve mejor el cuello real —PostgreSQL— y todas habrían
costado más tiempo de desarrollo en ETL, observabilidad y prototipado, que es donde el
reto exige amplitud.

### D2. FastAPI **[MVP]**

**Problema.** Exponer una API con contratos estrictos —montos, divisas, UUID, claves de
idempotencia— sin escribir validación a mano en cada endpoint.

**Decisión.** FastAPI en `servicio_transacciones`, `servicio_ia`, `servicio_bancs` y
`demo_web`.

**Rendimiento.** Es ASGI, con bajo overhead por petición, y —lo más relevante aquí—
permite **mezclar endpoints síncronos y asíncronos en la misma aplicación**. Los endpoints
de transferencia se declaran `def` (síncronos) y FastAPI los ejecuta en el threadpool de
AnyIO, lo que encaja con psycopg2, que es bloqueante. Declararlos `async def` con un driver
bloqueante habría bloqueado el bucle de eventos, que es un error de rendimiento clásico.
`demo_web` sí usa `async def`, porque sus llamadas son HTTP con `httpx` y ahí la asincronía
sí aporta.

**Seguridad.** La validación de Pydantic rechaza en el borde lo que nunca debería llegar a
la base: monto ≤ 0, divisa mal formada, UUID inválido, cuentas iguales. Eso produce un 422
sin abrir transacción, sin consumir una conexión del pool y sin ejecutar SQL. El contrato
explícito también reduce la superficie: lo que no está en el esquema, no entra.

**Escalabilidad.** Los servicios son *stateless* —el estado vive en PostgreSQL—, así que
admiten varios procesos y, con un balanceador delante, varias instancias. El límite no está
en el framework.

**Trade-off.** Usar FastAPI **no hace escalable al sistema**. La capacidad real la fija
PostgreSQL y el pool de conexiones. Tampoco mejora el trabajo CPU-bound: el threadpool de
AnyIO puede admitir 40 tareas por worker, pero solo 15 pueden sostener una conexión, así
que el límite práctico está antes del framework.

**Alternativas.** **Flask** (más simple, pero WSGI y sin validación integrada: habría que
añadir a mano lo que aquí es nativo), **Django** (ORM y admin que no se necesitan; su peso
no se justifica sin modelo de dominio rico), **gRPC** (mejor para comunicación interna de
alto volumen, pero el reto pide una API consumible y una demo web, y HTTP/JSON con OpenAPI
es directamente inspeccionable por un evaluador).

### D3. Varios workers de Uvicorn **[MVP]**

**Problema.** Un solo proceso Python no aprovecha varios núcleos por el GIL.

**Decisión.** `UVICORN_WORKERS` con valor por defecto **5**, definido en el `CMD` del
Dockerfile y en `docker-compose.yml`.

**Rendimiento.** Cada worker es un proceso independiente con su propio intérprete,
threadpool y pool de conexiones, lo que permite usar varios núcleos para peticiones
concurrentes.

**Seguridad / aislamiento.** Un fallo grave en un worker no arrastra a los demás.

**Escalabilidad.** Es el eje de escalado vertical del servicio. Pero **más workers no es
automáticamente más rendimiento**, y aquí está la relación que hay que saber explicar:

```text
conexiones totales = workers × (pool_size + max_overflow)
                   = 5 × (15 + 0) = 75
frente a max_connections = 200
```

Subir workers multiplica conexiones contra la misma base. Si el cuello está en contención
de bloqueos sobre las mismas cuentas, más workers **aumentan** la competencia por las
mismas filas y empeoran el resultado. El ajuste exige medir primero dónde está el límite
([06_incidente_critico.md](06_incidente_critico.md)).

**Trade-off.** Multiplica memoria y conexiones; además, al no estar configurado el modo
multiproceso de `prometheus_client`, cada scrape de `/metrics` refleja un solo worker
**[LIM]**.

---

## 9.3 Acceso a datos

### D4. psycopg2 con SQL explícito, sin ORM **[MVP]**

**Problema.** El camino financiero depende de construcciones SQL muy concretas cuyo
comportamiento debe ser exacto y predecible.

**Decisión.** psycopg2 directo en `servicio_transacciones`, `worker_outbox` y
`servicio_ia`. **No hay ORM**: SQLAlchemy no aparece en ningún `requirements.txt`.

**Rendimiento.** Control exacto del número de consultas —una transferencia completada son
ocho sentencias, ni una más—, uso de `RETURNING` para evitar releer después de escribir, y
la posibilidad de medir cada operación por separado (`smartbancs_db_sql_duracion_seconds`
etiquetada por `operacion`). Con un ORM, el número real de consultas depende de la
configuración de carga y es más difícil de auditar.

**Seguridad e integridad.** Todo el SQL está parametrizado (`%s` o parámetros con nombre);
no hay concatenación de cadenas. El control de `commit`/`rollback` es explícito, igual que
los `SAVEPOINT`. Y, sobre todo, **no hay ambigüedad sobre qué bloqueos se toman y cuándo**:
está escrito en la consulta. En un sistema de saldos, esa transparencia vale más que la
comodidad.

**Escalabilidad.** Permite gestionar el pool de forma propia (D6) y elimina la indirección
en la ruta más caliente.

**Trade-off.** Más código manual, más responsabilidad sobre el mapeo de filas, menor
portabilidad entre motores. Se asume deliberadamente: el sistema no pretende ser agnóstico
de base de datos, porque depende de características específicas de PostgreSQL.

**Alternativas.** **SQLAlchemy ORM** (productividad, a costa de opacidad sobre consultas y
bloqueos), **SQLAlchemy Core** (habría sido una opción razonable: SQL explícito con mejor
composición; se descartó por simplicidad de dependencias), **asyncpg** (más rápido y
asíncrono, pero habría obligado a un diseño `async` de extremo a extremo y a renunciar al
modelo síncrono que encaja con el threadpool).

### D5. Pool de conexiones propio, con cola acotada **[MVP]**

**Problema.** Abrir una conexión TCP y autenticar contra PostgreSQL en cada petición añade
latencia inaceptable para un objetivo de 2 segundos; pero un pool sin límite de espera
convierte un pico en una cola creciente de peticiones que ya no llegarán a tiempo.

**Decisión.** `ThreadedConnectionPool` de psycopg2 envuelto en `PoolConEspera`, que limita
los préstamos con un semáforo, admite una cola corta (`min(10, capacidad)`) y rechaza con
`PoolAgotado` si no hay slot en `DB_POOL_TIMEOUT_S` (0,05 s). El rechazo se traduce en
**503 con `Retry-After: 1`**.

**Rendimiento.** Reutilización de conexiones ya establecidas; el coste de conexión sale del
camino crítico. Además, el pool está instrumentado en cuatro puntos (espera del semáforo,
`getconn`, `putconn`, conexiones prestadas), lo que permite saber si el tiempo se va
esperando conexión o ejecutando SQL.

**Seguridad y estabilidad.** Acota cuántas conexiones puede consumir el servicio, evitando
agotar `max_connections`. Las conexiones se devuelven siempre limpias: `rollback()` antes de
reutilizar, y descarte (`close=True`) si la conexión quedó en estado desconocido o el
servidor cerró la sesión. Nunca se entrega a la siguiente petición una transacción abierta
o abortada.

**Escalabilidad.** Debe dimensionarse junto con los workers y `max_connections` (D3).

**Trade-off.** Un pool pequeño genera espera y 503 bajo carga; uno grande traslada la
presión a PostgreSQL y puede empeorar la contención. Se eligió **fallar rápido antes que
encolar**, que es la postura correcta en un sistema financiero: es preferible un rechazo
reintentable a una transferencia que expira.

---

## 9.4 Base de datos

### D6. PostgreSQL **[MVP]**

**Problema.** Saldos que no pueden quedar inconsistentes bajo concurrencia, con un libro de
movimientos auditable.

**Decisión.** PostgreSQL 16 como **única fuente de verdad** de saldos, transacciones,
movimientos, eventos de salida y análisis.

**Rendimiento.** Motor OLTP maduro: MVCC permite que lectores no bloqueen a escritores;
bloqueo a nivel de **fila**, de modo que transferencias sobre cuentas distintas avanzan en
paralelo; índices para los patrones de consulta conocidos, incluido un índice **parcial**
sobre los eventos `PENDIENTE` del outbox; y `pg_stat_statements` para saber qué sentencia
cuesta realmente.

**Seguridad e integridad.** Es el argumento central. Las garantías no dependen del código de
aplicación sino del motor: transacciones ACID, `CHECK (saldo >= 0)`,
`UNIQUE (id_idempotencia)`, `UNIQUE (transaccion_id, tipo)`, claves foráneas, tipos
enumerados y disparadores que hacen `movimientos` *append-only* e impiden revertir estados
finales. El rol de aplicación recibe `SELECT, INSERT, UPDATE` y **no** `DELETE`. Cualquier
cliente con acceso a la base queda sujeto a las mismas reglas.

**Escalabilidad.** Alta concurrencia vertical en un nodo; como evolución, **réplicas de
lectura** para consultas y, si el volumen lo exigiera, particionamiento de `movimientos` y
`eventos_outbox` por fecha, que son las tablas que solo crecen **[PROD]**.

**Trade-off.** El nodo de escritura es el **límite central del sistema**, y hay que decirlo
sin rodeos: se puede escalar la API, los workers y los consumidores, pero todos escriben
contra la misma instancia. `max_connections`, la contención de bloqueos sobre cuentas
calientes y el tuning son los frenos reales. Además, en el MVP es una **instancia única sin
réplica ni failover** **[LIM]**.

**Alternativas.** **MySQL/MariaDB** (habría servido; PostgreSQL se prefirió por
`FOR NO KEY UPDATE`, `SKIP LOCKED`, índices parciales, `jsonb` y `pg_stat_statements`,
todos usados aquí). **SQL Server** (descartado por licenciamiento en un MVP). **NoSQL**
(MongoDB, DynamoDB, Cassandra): descartado para saldos porque el caso de uso exige
**atomicidad multi-fila** —debitar una cuenta, acreditar otra, escribir dos movimientos,
cambiar un estado y emitir un evento, todo o nada— y consistencia fuerte inmediata. Los
modelos de consistencia eventual obligarían a implementar en la aplicación lo que aquí da
el motor, con más código y más formas de fallar. Para un libro de saldos, eso es cambiar
garantías por escalado que este volumen no necesita.

### D7. ACID como decisión, no como etiqueta **[MVP]**

**Atomicidad.** Los cinco efectos —saldos, movimientos, estado de la transacción, fila de
idempotencia y evento outbox— ocurren en **una sola transacción**. Es lo que hace que el
outbox funcione (D11) y que cancelar una sesión durante un incidente sea seguro: se revierte
todo.

**Consistencia.** Verificada en el esquema real: `CHECK` de saldo y monto, cuentas
distintas, formato de divisa, `UNIQUE` de idempotencia y de tipo de movimiento, claves
foráneas y triggers de transición de estado.

**Aislamiento.** El nivel es el **`READ COMMITTED` por defecto de PostgreSQL**: el proyecto
**no lo cambia en ningún punto** (no existe `SET TRANSACTION` ni configuración de
`isolation_level` en el repositorio). El aislamiento efectivo del saldo no proviene del
nivel sino de los bloqueos de fila que toma el propio `UPDATE` condicionado (D8).

**Durabilidad.** Al confirmar, PostgreSQL persiste según su configuración **por defecto**.
El proyecto **no modifica** `synchronous_commit`, `fsync` ni ningún parámetro de WAL, así
que aplican los valores por defecto de la imagen `postgres:16-alpine`. No se afirma nada
más, porque nada más está configurado. Los datos viven en el volumen `postgres_data`.

### D8. Débito condicionado en lugar de bloqueo previo **[MVP]**

**Problema.** Evitar el doble gasto sin pagar el coste de bloquear ambas cuentas en cada
transferencia.

**Decisión.** El camino normal **no ejecuta `SELECT ... FOR UPDATE`**. El débito es:

```sql
UPDATE cuentas SET saldo = saldo - %s
WHERE id_cuenta = %s AND estado = 'ACTIVA' AND divisa = %s AND saldo >= %s
RETURNING saldo
```

El `UPDATE` bloquea la fila y evalúa la condición sobre la versión confirmada en una sola
operación atómica. Si no afecta filas, se vuelve al savepoint y **solo entonces** se ejecuta
`SELECT ... ORDER BY id_cuenta FOR NO KEY UPDATE` para clasificar el motivo del rechazo.

**Rendimiento.** El caso mayoritario hace menos viajes a la base y sostiene los bloqueos
menos tiempo: se toman en el `UPDATE` y se liberan en el `COMMIT`, sin SQL posterior. El
caso de rechazo paga una consulta adicional, que es el reparto correcto del coste.

**Seguridad e integridad.** Elimina la ventana leer-modificar-escribir: no hay momento entre
comprobar el saldo y descontarlo. Dos transferencias sobre la misma cuenta se serializan en
esa fila. El `CHECK (saldo >= 0)` respalda la regla desde el motor.

**Escalabilidad.** Funciona igual con varios workers e instancias, porque el árbitro es la
fila de la base. Pero **las cuentas calientes limitan el paralelismo**: todas las
transferencias sobre la misma cuenta origen se serializan, por diseño.

**Trade-off.** Consistencia fuerte frente a máximo throughput. Es el trade-off que un
sistema de saldos debe elegir en esa dirección.

**Por qué `FOR NO KEY UPDATE` y no `FOR UPDATE`** en el fallback: la operación no modifica
columnas de clave, y `FOR NO KEY UPDATE` es un modo más débil que **no entra en conflicto**
con los bloqueos que toman las claves foráneas que apuntan a `cuentas`. Da la garantía
necesaria con menos contención.

### D9. Orden determinista de actualización **[MVP]**

**Problema.** Dos transferencias cruzadas (A→B y B→A simultáneas) que tomasen los bloqueos
en orden opuesto producirían un deadlock.

**Decisión.** Las cuentas se procesan ordenadas por UUID (`sorted(...)`) —aunque eso
signifique acreditar el destino antes de debitar el origen— y la consulta de respaldo usa
`ORDER BY id_cuenta`.

**Efecto.** **Reduce el riesgo de deadlock**; no lo elimina. Pueden producirse deadlocks por
otras vías (otras tablas, otras sesiones, otros patrones), y por eso el servicio los trata
explícitamente: `40P01` se reintenta hasta 3 veces con backoff exponencial y jitter, y se
cuenta en `smartbancs_db_deadlocks_total`. **Prevención y diagnóstico son cosas distintas y
ambas están implementadas.**

**Coste.** Acreditar primero al destino obliga a poder deshacer ese crédito si el débito
falla, que es exactamente lo que resuelve el `SAVEPOINT sp_transferencia_saldos` (D10).
Cubierto por `test_destino_uuid_menor_debito_fallido_revierte_credito`.

### D10. Savepoints con propósito acotado **[MVP]**

**Decisión.** Dos savepoints, cada uno para un problema concreto:

- **`sp_idempotencia`**: absorbe la `UniqueViolation` cuando dos peticiones concurrentes
  usan la misma clave. Sin él, PostgreSQL abortaría **toda** la transacción y habría que
  empezar de cero; con él, se vuelve al punto anterior, se relee la fila ganadora y se
  devuelve su resultado.
- **`sp_transferencia_saldos`**: deshace un crédito ya aplicado cuando el débito falla,
  conservando la fila de transacción ya creada.

**Trade-off y límite.** Un savepoint tiene coste en el servidor y **no es un mecanismo
general de manejo de errores**: usarlos en exceso degrada el rendimiento. Aquí hay
exactamente dos, cada uno resolviendo un caso que de otro modo obligaría a abortar y
reintentar la transacción completa.

### D11. `Decimal` en Python y `NUMERIC(18,2)` en PostgreSQL **[MVP]**

**Problema.** El punto flotante binario no representa exactamente valores decimales: `0.1 +
0.2` no es `0.3`. Acumulado sobre saldos, produce descuadres.

**Decisión.** `Decimal` en el esquema Pydantic y en la lógica, con
`quantize(Decimal("0.01"))` antes de operar, y `numeric(18,2)` en las columnas de saldo,
monto y saldo resultante. El hash de idempotencia se calcula sobre el monto ya cuantizado,
para que dos representaciones del mismo importe produzcan el mismo hash.

**Rendimiento.** `Decimal` es más lento que `float`, y `NUMERIC` más lento que `bigint` o
`double precision`. El coste es **despreciable frente al resto de la operación**: el tiempo
de una transferencia lo domina la E/S con la base, no la aritmética de dos importes.

**Seguridad e integridad.** Es el argumento decisivo: exactitud monetaria. Un error de
redondeo en un sistema de saldos no es un problema de rendimiento, es un problema de
dinero.

**Escalabilidad.** Impacto no significativo a este volumen.

**Alternativa.** `float`/`double`: descartada por representación inexacta. Otra alternativa
habitual es **guardar centavos como entero**, que es exacta y más rápida; no se eligió
porque `NUMERIC` ya da exactitud, expresa la escala en el propio esquema y evita errores de
conversión en cada borde del sistema.

### D12. UUID como clave primaria **[MVP]**

**Decisión.** `UUID` con `gen_random_uuid()` (extensión `pgcrypto`) como clave primaria de
todas las tablas, con `numero_cuenta` como clave natural única.

**Ventajas aplicadas aquí.** Generación sin coordinación central (cualquier proceso puede
crear una fila sin pedir un número a una secuencia), y **no se exponen identificadores
consecutivos**: la interfaz trabaja con `numero_cuenta` y `demo_web` filtra los UUID de las
respuestas al navegador.

**Uso adicional, no evidente.** El orden lexicográfico de los UUID canónicos es el que fija
el **orden determinista de bloqueo** (D9). El identificador cumple una función arquitectónica
más allá de identificar.

**Trade-off.** 16 bytes frente a 4 u 8 de un entero: índices más grandes y **peor localidad**
que una secuencia, porque los UUID v4 son aleatorios y dispersan las escrituras en el índice.
A este volumen no es un problema; en un sistema mucho mayor podría considerarse una variante
ordenable en el tiempo (UUIDv7).

**Matiz honesto.** No exponer identificadores consecutivos **dificulta** la enumeración, pero
no es un control de seguridad por sí mismo: sin autenticación, la protección real sigue
faltando (D22).

---

## 9.5 Concurrencia e idempotencia

### D13. Clave de idempotencia + hash SHA-256 + `UNIQUE` **[MVP]**

**Problema.** El cliente puede repetir la misma solicitud por timeout, reintento automático,
corte de red o doble clic. Una transferencia no es una operación repetible.

**Decisión.** Tres piezas que trabajan juntas:

1. **`id_idempotencia`** que envía el cliente (`demo_web` genera un UUID si el frontend no
   la aporta).
2. **Hash SHA-256** del contenido canónico: `origen|destino|monto(2 decimales)|DIVISA`.
3. **`UNIQUE (id_idempotencia)`** en la tabla, que es el árbitro atómico bajo concurrencia.

Misma clave + mismo hash → se devuelve la transacción almacenada con `reproducida = true`,
sin mover saldos. Misma clave + hash distinto → **409**.

**Rendimiento.** No hay consulta previa fuera de la transacción: el `SELECT` y el `INSERT`
ocurren dentro de la misma transacción que el resto, y el `UNIQUE` resuelve el empate. Así,
el caso mayoritario (clave nueva) **no paga viajes adicionales**; solo los duplicados, que
son raros, pagan la relectura. Además, una reproducción evita ejecutar de nuevo toda la
transferencia.

**Seguridad e integridad.** Es lo que impide duplicar movimientos y, en consecuencia, lo que
hace **segura la recuperación ante incidentes**: el 503 del sistema dice explícitamente
"reintente con la misma clave de idempotencia", y eso solo puede decirse porque esta
decisión existe.

**Escalabilidad.** La protección vive en la base de datos, compartida, así que funciona con
cualquier número de workers e instancias. Una solución en memoria del proceso no lo haría.

**Sobre SHA-256.** Su papel aquí es producir una **huella determinista del payload**, no
proteger secretos. No es *password hashing* y **no cifra nada**: cualquier función hash
estable habría servido; se eligió SHA-256 por estar en la biblioteca estándar y tener riesgo
práctico de colisión despreciable para este uso.

**Trade-off.** Una columna `char(64)` adicional por transacción, lógica extra y la necesidad
de que el cliente gestione sus claves. Es un coste pequeño frente a la alternativa de
duplicar dinero.

### D14. Límites de tiempo definidos en el rol de PostgreSQL **[MVP]**

**Decisión.** En `ddl.sql`, sobre el rol `svc_banc_user`: `lock_timeout = 800ms`,
`statement_timeout = 1500ms`, `idle_in_transaction_session_timeout = 5s`.

**Por qué en el rol y no en el código.** Aplican a **toda** conexión del servicio aunque el
código cambie o alguien abra una sesión a mano. Es una garantía del sistema, no una
convención de programación.

**Rendimiento.** Convierten una espera indefinida en un error rápido con SQLSTATE conocido
(`55P03`, `57014`), que el servicio ya sabe clasificar y reintentar. Acotan el peor caso de
latencia, que es lo que importa para un objetivo de 2 segundos.

**Integridad.** `idle_in_transaction_session_timeout` corta sesiones que retienen bloqueos
sin avanzar, que es el escenario más dañino en un pico.

**Trade-off.** Bajo carga legítima alta, un timeout puede cortar trabajo que habría
terminado. Se acepta: es preferible un rechazo reintentable a una transferencia que expira
en el cliente.

### D15. Reintentos acotados con backoff y jitter **[MVP]**

**Decisión.** Hasta 3 intentos de la operación **completa** ante `40P01` (deadlock), `55P03`
(lock timeout) y `57014` (statement timeout), con espera `0.025 × 2^(intento-1)` segundos
multiplicada por un factor aleatorio entre 0,5 y 1,5. Cualquier otro SQLSTATE **no se
reintenta**: se propaga como 500.

**Por qué solo esos tres.** Son los casos en que PostgreSQL ya revirtió la transacción
entera, de modo que repetirla es seguro. Reintentar un error de datos sería repetir un fallo.

**Por qué jitter.** Sin aleatoriedad, varios clientes que colisionan reintentan a la vez y
vuelven a colisionar. El jitter dispersa los reintentos.

**Trade-off.** Un reintento consume capacidad; por eso están acotados y por eso, agotados,
se devuelve 503 en lugar de seguir insistiendo.

---

## 9.6 Asincronía

### D16. Transactional Outbox **[MVP]**

**Problema (doble escritura).** Confirmar la transferencia en PostgreSQL y publicar en
RabbitMQ son escrituras en sistemas distintos. La secuencia ingenua —`COMMIT`, luego
publicar— deja una transferencia confirmada sin evento si la publicación falla. Invertirla
publica eventos de transferencias que quizá no se confirmen.

**Decisión.** El evento se inserta en `eventos_outbox` **dentro de la misma transacción**
que mueve los saldos. Un proceso aparte lo publica después, reclamando lotes con
`FOR UPDATE SKIP LOCKED`.

**Rendimiento.** El camino crítico **no habla con el broker**: dentro del commit solo hay un
`INSERT` local. La latencia de la transferencia no depende de RabbitMQ ni de su
disponibilidad.

**Seguridad e integridad.** Garantía verificable: **si la transferencia está confirmada, su
evento existe**. Si la transferencia se revierte, el evento desaparece con ella.

**Escalabilidad.** La publicación está desacoplada y el patrón `SKIP LOCKED` permite varios
workers repartiéndose eventos sin duplicar trabajo.

**Trade-off.** Consistencia eventual del evento, un proceso más que operar, entrega
*at-least-once* (posibles duplicados) y, por tanto, necesidad de que el consumidor los
tolere. En el MVP eso lo resuelve el `UNIQUE (transaccion_id)` de `analisis_ia` con un
*upsert*, no una deduplicación explícita **[LIM]**. Además, la entrega no es inmediata:
depende del sondeo de 1 segundo del worker.

**Alternativa.** *Change Data Capture* sobre el WAL (Debezium): evita el sondeo y no toca el
código de aplicación, a costa de infraestructura y operación considerables. Desproporcionado
para este alcance.

### D17. RabbitMQ **[MVP]**

**Problema.** Transportar los eventos hasta el consumidor de IA sin acoplar productor y
consumidor.

**Decisión.** RabbitMQ con cola `cola_ia` **durable**, mensajes persistentes
(`delivery_mode=2`), `confirm_delivery()` y `mandatory=True` en la publicación;
`prefetch_count=1` y `ack` manual en el consumo.

**Rendimiento.** Saca de la petición todo el trabajo no crítico y **absorbe picos**: una
ráfaga de transferencias se convierte en cola, no en errores ni en latencia para el usuario.

**Seguridad y resiliencia.** Desacopla los ciclos de vida: una caída de la IA no puede
provocar un rollback financiero, porque la transferencia ya se confirmó. El `ack` manual
garantiza que un mensaje no se dé por procesado hasta que realmente lo esté.

**Escalabilidad.** Varios consumidores pueden repartirse la cola sin cambios en el
productor.

**Trade-off.** Un componente más que operar y vigilar, consistencia eventual del análisis,
gestión de backlog y de reintentos. Y una carencia concreta: **no hay DLQ**; el consumidor
descarta con `basic_nack(requeue=False)` **[LIM]**.

**Alternativas.** **Kafka** (mejor para altísimo volumen, retención larga y reproceso
histórico; aporta complejidad operativa que este flujo punto a punto no necesita).
**Redis Streams** (más ligero, pero con menos garantías de durabilidad y enrutamiento; en un
sistema financiero no compensa). **Procesamiento síncrono** (descartado: es exactamente lo
que el reto prohíbe, porque la IA bloquearía la transferencia).

### D18. La IA fuera del camino crítico **[MVP]**

**Problema.** El reto exige que las recomendaciones de IA no bloqueen ni retrasen el flujo
transaccional.

**Decisión.** El análisis se ejecuta en `consumidor_ia`, a partir del mensaje de la cola. El
servicio transaccional **no conoce** al de IA: no hay import, ni llamada HTTP, ni dependencia
de arranque. La interfaz consulta el resultado después y muestra `PENDIENTE` mientras no
exista.

**Rendimiento.** La latencia de la inferencia, sea cual sea, no la paga el usuario.

**Seguridad y aislamiento.** Ninguna ruta de error de la IA alcanza el endpoint financiero.
Una caída del consumidor retrasa análisis, nunca rechaza transferencias.

**Escalabilidad.** El número de consumidores es independiente del de workers de la API.

**Trade-off.** Recomendación eventual, un componente más, y la necesidad de vigilar el
backlog.

**Precisión que evita una afirmación falsa [MVP].** La **API** `servicio_ia` es un servicio
separado, pero la **inferencia no se invoca por HTTP**: `consumidor_ia` importa
`analizar_transaccion` y la ejecuta en su propio proceso. El puerto 8002 sirve para
*consultar* análisis persistidos, no para producirlos. El desacoplamiento real lo aporta la
cola, no una llamada de red.

---

## 9.7 Procesamiento de datos

### D19. Pandas para el ETL **[MVP]**

**Problema.** Normalizar CSV heterogéneos —delimitadores distintos, decimales con coma,
fechas en varios formatos, divisas en minúscula— y separar lo válido de lo observado.

**Decisión.** Pandas, en un proceso batch de un solo archivo.

**Rendimiento.** Suficiente para el volumen del MVP (los datasets incluidos tienen 1.999
registros; la interfaz limita las cargas a 20 MB). El coste de arranque de alternativas
distribuidas superaría con creces el del procesamiento.

**Seguridad y calidad de datos.** Permite expresar con poco código las reglas de validación
y, sobre todo, **separar los registros observados con su motivo de rechazo**, que es lo que
hace auditable el proceso ([03_bancs_etl.md](03_bancs_etl.md)).

**Escalabilidad.** Limitada por memoria: todo el archivo se carga en un DataFrame. Es el
límite conocido y aceptado **[LIM]**.

**Alternativas.** **Polars** (más rápido y con menor consumo; no se priorizó porque Pandas
resuelve el volumen actual y es más conocido para quien revise el código). **Spark**
(desproporcionado: el coste operativo de un clúster para miles de filas no se justifica;
sería una arquitectura de Big Data ficticia). **SQL puro** (obligaría a cargar antes los
datos sucios en la base, que es justo lo que el ETL debe evitar). **Streaming**
(innecesario para un proceso que se dispara a mano con un archivo).

---

## 9.8 Entorno y configuración

### D20. Docker **[MVP]**

**Decisión.** Cada componente en su propia imagen, con Python 3.12 slim.

**Rendimiento.** **No se justifica por rendimiento**: la contenerización añade una capa, no
la quita. Su valor es otro.

**Valor real.** Reproducibilidad (mismas versiones en cualquier máquina), aislamiento de
dependencias entre servicios con requirements distintos, y consistencia entre lo que se
desarrolla y lo que se evalúa.

**Seguridad.** Aislamiento de procesos y sistema de archivos, red interna donde los
servicios se llaman por nombre sin exponer puertos innecesarios, y configuración por
variables de entorno en lugar de ficheros con secretos dentro de la imagen.

**Escalabilidad.** Es la base para cualquier orquestación posterior; sin imágenes no hay
despliegue replicable.

**Trade-off.** Consumo adicional de recursos y complejidad operativa. En el MVP hay un
detalle concreto: **los `container_name` fijos impiden usar `docker compose --scale`**
**[LIM]**.

### D21. Docker Compose **[MVP]**

**Problema.** El reto pide poder levantar el entorno con un solo comando.

**Decisión.** Un `docker-compose.yml` con los once servicios, sus dependencias, el
healthcheck de PostgreSQL, los volúmenes y la red compartida. `docker compose up -d --build`
levanta base de datos, servicios, mensajería y la pila de observabilidad ya aprovisionada.

**Por qué es apropiado aquí.** Es la herramienta proporcionada al alcance: un evaluador
reproduce el entorno completo sin conocimiento previo del proyecto, y el orden de arranque
está expresado de forma declarativa (`depends_on` con `service_healthy`).

**Trade-off y límite.** **No es una solución de orquestación productiva**: no hay
autoescalado, ni despliegue progresivo, ni reprogramación ante fallo de nodo, ni gestión de
secretos. Además, solo `postgres` tiene healthcheck y solo dos servicios declaran política
de reinicio **[LIM]**.

**Alternativa.** **Kubernetes**, que sí aporta escalado, autorreparación, despliegues
progresivos y gestión de secretos. No se priorizó porque para un MVP de un solo host
añadiría manifiestos, un clúster local y una curva operativa que no aportan nada a lo que el
reto evalúa; y porque la barrera de entrada para reproducir el entorno sería mucho mayor. La
migración es natural cuando llegue el momento: los servicios ya están contenerizados,
configurados por entorno y son *stateless*.

### D22. Configuración por variables de entorno **[MVP]**

**Decisión.** `pydantic-settings` en `servicio_transacciones` y `worker_outbox` (con
validación de tipos al arrancar), `os.environ` en `servicio_ia` y `demo_web`. `.env` en
`.gitignore`, con `.env.example` versionado.

**Seguridad.** Evita credenciales incrustadas en el código o en las imágenes, y permite
separar configuración por entorno. Hay además una separación real de usuarios de base de
datos: el superusuario solo se usa en la inicialización, y los servicios se conectan con un
rol de permisos acotados.

**Aclaración necesaria.** **Un archivo `.env` no es un sistema de gestión de secretos**. Es
un fichero en claro en el host, visible en `docker inspect` a través del entorno del
contenedor. Para producción correspondería un gestor de secretos con rotación y auditoría
**[PROD]**.

**[LIM] relacionado.** No existe autenticación ni autorización en ningún endpoint. Es la
carencia de seguridad más importante del MVP y se declara como tal, sin disfrazarla.

---

## 9.9 Observabilidad

El detalle está en [05_observabilidad.md](05_observabilidad.md). Aquí, solo el porqué.

### D23. Logs estructurados en JSON **[MVP]**

**Decisión.** Formateador JSON propio, con campos fijos y un conjunto acotado de campos
opcionales (`evento`, `trace_id`, `id_transaccion`, `sqlstate`, `operacion`, `motivo`,
`duracion_ms`, `tipo_error`).

**Rendimiento.** Un log por operación relevante, no por paso: el camino crítico no se llena
de escritura. El formato estructurado permite filtrar por máquina sin expresiones regulares
frágiles.

**Seguridad.** Punto verificado: **el camino activo no registra importes, números de cuenta
ni datos del titular**. Se registran identificadores técnicos, estados y códigos de error.
Un log estructurado facilita además auditar qué campos se emiten.

**Escalabilidad.** JSON se integra directamente con cualquier plataforma de centralización.

**Trade-off.** Exige disciplina de esquema, y aquí no es homogénea: el worker y el consumidor
**no incluyen timestamp ni nombre de logger**, y mezclan `print()` con logging estructurado
**[LIM]**.

### D24. `prometheus_client` instrumentando a mano **[MVP]**

**Decisión.** Instrumentación explícita con `Counter`, `Histogram` y `Gauge` en los puntos
que importan, en lugar de depender solo de métricas genéricas de framework.

**Por qué.** Las preguntas operativas de este sistema son específicas: *¿se agotó el pool?*,
*¿cuánto se espera por un bloqueo?*, *¿qué operación SQL se degradó?*, *¿hay eventos de
outbox fallidos?*. Ninguna la responde una métrica genérica de HTTP. La instrumentación
sigue la estructura del problema, de fuera hacia dentro, y eso es lo que permite localizar
el escalón donde se va el tiempo.

**Seguridad y coste.** Decisión deliberada de **cardinalidad acotada**: las etiquetas son
`worker`, `endpoint`, `status`, `tipo`, `motivo`, `operacion`, `sqlstate`, `estado`,
`resultado`. **Nunca se usan identificadores de transacción, de cuenta ni de idempotencia
como etiquetas**, y el `motivo` de los 503 se restringe a una lista cerrada. Esto evita la
explosión de series temporales, que es la forma habitual de tumbar un Prometheus, y evita
filtrar datos de negocio a un sistema de métricas.

**Trade-off.** Instrumentar a mano cuesta código y mantenimiento, y medir tiene un coste en
el camino crítico. Un matiz: la etiqueta `worker` es el PID, que **cambia en cada reinicio**
y crea series nuevas; es cardinalidad acotada en una ejecución, pero creciente a lo largo del
tiempo **[LIM]**.

### D25. Prometheus y Grafana, con papeles distintos **[MVP]**

**Prometheus** recolecta por *pull* cada 5 segundos, almacena las series y responde
consultas PromQL. El modelo de extracción evita agentes adicionales y hace que un servicio
caído se note por ausencia de scrape.

**Grafana** **no recolecta nada**: consulta a Prometheus y visualiza. Su valor es el
diagnóstico rápido y la correlación visual entre señales que, por separado, no dicen lo
mismo: throughput plano + latencia creciente + errores estables es un cuadro distinto de
throughput plano + errores crecientes.

**Escalabilidad y trade-offs.** Prometheus en un nodo es suficiente para este alcance; a
mayor escala correspondería almacenamiento remoto y retención acordada. Su punto débil es la
alta cardinalidad (D24). Y una carencia real: **el endpoint `/metrics` y las interfaces de
Prometheus y Grafana están expuestos sin autenticación** **[LIM]**; en producción deberían
restringirse.

### D26. OpenTelemetry **[MVP]**

**Decisión.** OTel como capa de instrumentación de trazas, con exportador OTLP gRPC, en los
tres procesos del flujo.

**Por qué el estándar y no un SDK propietario.** Es neutral: el backend de trazas puede
cambiarse sin tocar el código instrumentado. En un reto que pide una solución
tecnológicamente agnóstica, esa propiedad es exactamente la que se busca.

**Rendimiento.** `BatchSpanProcessor` agrupa el envío en lugar de exportar span a span. El
overhead es controlable con muestreo **[PROD]**: hoy se exportan todas las trazas, lo que a
volumen alto sería costoso **[LIM]**. El servicio permite desactivar el exportador con
`OTEL_TRACES_EXPORTER=none`, que es lo que usan las pruebas.

**Seguridad.** Verificado: **los atributos de los spans no contienen importes ni números de
cuenta**. Llevan duraciones, tipos de operación, sistema de mensajería, número de cuentas
bloqueadas, número de movimientos, modelo y nivel de IA. Es la decisión correcta: una traza
no debe convertirse en un canal alternativo de datos financieros.

**Escalabilidad.** La propagación W3C es interoperable, y el contexto sobrevive a la tabla
de outbox y a la cola, que es lo que permite una única traza de extremo a extremo.

**Trade-off.** Instrumentar y almacenar trazas tiene coste; y aquí hay un efecto secundario
reconocido: conviven **dos identificadores de correlación** (`X-Trace-Id` de aplicación y el
trace ID de OTel) con el mismo nombre de campo en los logs **[LIM]**.

### D27. Jaeger **[MVP]**

**Decisión.** `jaegertracing/all-in-one` como backend de trazas del MVP.

**Valor.** Permite ver la jerarquía padre/hijo, la duración de cada span y los atributos, y
responde la pregunta que ninguna métrica responde: **en esta transferencia concreta, ¿dónde
se fue el tiempo?**. Un hueco antes del primer span de base de datos significa espera por
conexión; un `db.actualizar_saldos` dominante significa contención.

**Trade-off.** *All-in-one* almacena **en memoria**: las trazas se pierden al reiniciar
**[LIM]**, lo que lo hace inútil para investigar un incidente pasado. Para producción
correspondería un despliegue con almacenamiento persistente y retención acordada, o una
plataforma equivalente **[PROD]**.

### D28. Herramientas nativas de PostgreSQL para diagnóstico **[MVP]**

**Decisión.** Habilitar `pg_stat_statements` (con `track=all`), `log_lock_waits=on`,
`deadlock_timeout=200ms`, `log_min_duration_statement=250ms` y un `log_line_prefix` que
incluye `application_name`; y conectar cada componente con su propio `application_name`
(`smartbancs`, `worker_outbox`, `consumidor_ia`).

**Por qué cada una:**

- **`pg_stat_statements`** da la visión **acumulada**: qué sentencia domina el tiempo total
  por frecuencia × coste, que a menudo no es la más lenta individualmente. Su overhead es
  pequeño y su valor operativo alto. Como el SQL está escrito a mano y es estable, las
  sentencias se agrupan limpiamente.
- **`pg_stat_activity`** da la visión **instantánea**: qué se está ejecutando ahora, desde
  cuándo y esperando qué. No es una herramienta de histórico y no debe presentarse como tal.
- **`pg_locks`** (con `pg_blocking_pids`) responde **quién bloquea a quién**. Por sí mismo
  **no previene deadlocks ni contención**: es diagnóstico, no prevención. La prevención es
  el orden determinista (D9).

**Trade-off.** No hay *exporter* de PostgreSQL, así que nada de esto llega a Prometheus: es
diagnóstico **manual**, y depende de que alguien sepa qué consultar **[LIM]**. Las consultas
están escritas en [06_incidente_critico.md](06_incidente_critico.md).

---

## 9.10 Pruebas

### D29. pytest **[MVP]**

**Decisión.** 20 módulos de prueba, en el mismo lenguaje que el código, incluyendo pruebas
de **integración contra PostgreSQL real** y de **concurrencia con hilos y barreras**.

**Por qué importa la elección de nivel.** Las garantías críticas de este sistema —que no
haya doble gasto, que el crédito se revierta si falla el débito, que dos workers no reclamen
el mismo evento— **no se pueden verificar con dobles de prueba**: dependen del comportamiento
real de los bloqueos de PostgreSQL. Por eso esas pruebas usan base de datos real y
`threading.Barrier` para forzar simultaneidad efectiva.

**Trade-off.** Las pruebas de integración necesitan el entorno levantado y son más lentas; y
**no hay `skipif`**, así que sin PostgreSQL fallan en lugar de omitirse **[LIM]**.

### D30. k6 para carga **[MVP]**

**Decisión.** k6 con ejecutor `constant-arrival-rate`, dos datasets (concentrado y
disperso) y thresholds de latencia y error.

**Por qué `constant-arrival-rate`.** Modela **llegadas**, no usuarios: mantiene la tasa
solicitada con independencia de lo que tarde el sistema. Es como se comporta un pico real de
tráfico. Un ejecutor de VUs fijos se autolimita cuando el sistema se ralentiza y, por tanto,
oculta la saturación en lugar de medirla.

**Por qué dos datasets.** **HOT** concentra el tráfico en pocas cuentas y mide el coste de
la contención; **LOAD** lo dispersa sobre 5.000 y mide capacidad con bloqueos dispersos.
Comparar ambos al mismo `RATE` cuantifica cuánto cuesta la contención, que es la pregunta
específica de este diseño.

**Threshold `p(95)<2000`** traduce directamente el requisito de "menos de 2 segundos" del
reto; `http_req_failed: rate<0.01` acota el error.

**Seguridad.** Los datasets contienen únicamente identificadores de cuentas de prueba; no
hay credenciales ni datos personales. Como advertencia general: un dataset de carga nunca
debería llevar identificadores productivos.

**Escalabilidad de la propia prueba.** k6 admite ejecución distribuida para escenarios
mayores; en el MVP se ejecuta local, lo que significa que **el generador compite por CPU con
el sistema medido** **[LIM]**.

**Alternativas.** **JMeter** (potente, pero configuración pesada en XML), **Locust**
(escenarios en Python, con el generador limitado por el propio GIL), **Gatling** (excelente,
pero requiere Scala/Java). k6 se eligió por escenarios en JavaScript, salida clara y
thresholds declarativos.

**No se afirma capacidad.** El repositorio no conserva resultados versionados, así que **no
se sostiene ninguna cifra de TPS** ([08_pruebas_rendimiento.md](08_pruebas_rendimiento.md)).

---

## 9.11 Separación en servicios

**Decisión.** Servicios separados **solo donde la separación aporta algo**, no por doctrina:

| Componente | Por qué separado |
|---|---|
| `servicio_transacciones` | Es el camino crítico. Debe poder escalarse, medirse y desplegarse sin arrastrar nada más |
| `worker_outbox` | Proceso de fondo con ciclo de vida distinto: se puede pausar durante un incidente sin afectar transferencias |
| `consumidor_ia` | Aísla el análisis; su caída no puede afectar al dinero |
| `servicio_ia` (API) | Permite consultar análisis sin depender del consumidor |
| `servicio_bancs` | Simula un sistema externo cuyo ciclo de vida no controlamos |
| `demo_web` | Fachada de presentación, sin lógica de negocio, que puede cambiar sin tocar el núcleo |

**Beneficios reales aquí.** Aislamiento de fallos (demostrable: pausar el consumidor no
afecta a las transferencias), despliegue independiente y escalabilidad diferenciada.

**Costos asumidos.** Llamadas de red donde antes habría llamadas en proceso, trazabilidad
distribuida necesaria para entender una operación, más contenedores que operar y
consistencia eventual entre partes.

**Lo que no se hizo, y es deliberado.** No se partió el núcleo transaccional en servicios más
pequeños (cuentas, movimientos, transacciones por separado). Habría sido el error clásico:
repartir una **transacción ACID** entre servicios obligaría a transacciones distribuidas o
sagas, con compensaciones sobre saldos, para resolver un problema que una sola transacción
de base de datos resuelve de forma trivial. La atomicidad financiera es el criterio que fija
dónde **no** debe pasar una frontera de servicio.

---

## 9.12 Decisiones no tomadas

| Alternativa | Por qué no se priorizó |
|---|---|
| **Kubernetes** | Para un MVP de un solo host añade clúster, manifiestos y curva operativa sin aportar a lo evaluado, y dificulta que un evaluador reproduzca el entorno. Los servicios ya están listos para migrar |
| **Kafka** | Pensado para volumen muy alto, retención larga y reproceso histórico. Este flujo es punto a punto con un consumidor; RabbitMQ lo cubre con menos operación |
| **NoSQL para saldos** | El caso exige atomicidad multi-fila y consistencia fuerte inmediata. Implicaría reimplementar en la aplicación lo que el motor relacional garantiza |
| **ORM en el camino financiero** | Menos control explícito sobre consultas, bloqueos y transacciones, justo donde ese control es el requisito |
| **Redis como fuente de verdad del saldo** | Añadiría un segundo sistema con su propio modelo de consistencia y durabilidad; el saldo dejaría de tener un único dueño. Como **caché de lectura** sería razonable; como fuente de verdad, no |
| **`async` de extremo a extremo con asyncpg** | Habría exigido reescribir el acceso a datos; el modelo síncrono sobre threadpool rinde bien cuando el límite es la base de datos, no el bucle de eventos |
| **Centavos como enteros** | Alternativa exacta y válida; `NUMERIC` ya da exactitud y expresa la escala en el esquema, evitando conversiones en cada borde |
| **Saga / transacciones distribuidas** | Innecesarias: la operación financiera cabe en una sola transacción local. Introducirlas sería complejidad sin beneficio |
| **Spark para el ETL** | Desproporcionado para miles de filas; sería una arquitectura de datos ficticia |

---

## 9.13 Cómo encajan las decisiones

Ninguna se sostiene sola:

```text
FastAPI valida en el borde          → lo inválido nunca llega a la base ni consume conexión
   ↓
Pool con cola acotada               → protege PostgreSQL; rechaza rápido antes que encolar
   ↓
psycopg2 con SQL explícito          → control exacto de consultas, bloqueos y transacción
   ↓
PostgreSQL garantiza ACID           → atomicidad de saldos, movimientos, estado y evento
   ├─ Decimal/NUMERIC               → exactitud monetaria
   ├─ UPDATE condicionado           → sin race condition ni doble gasto
   ├─ Orden determinista (UUID)     → reduce el riesgo de deadlock
   ├─ SAVEPOINT                     → absorbe colisiones sin abortar la transacción
   ├─ UNIQUE + hash                 → idempotencia: el reintento del cliente es seguro
   └─ Timeouts de rol + reintentos  → el peor caso está acotado
   ↓
Outbox en la misma transacción      → si la transferencia existe, su evento existe
   ↓
RabbitMQ                            → desacopla; absorbe picos; la IA no bloquea
   ↓
Prometheus + Grafana                → comportamiento agregado: ¿algo va mal y dónde?
OpenTelemetry + Jaeger              → una operación concreta: ¿dónde se fue el tiempo?
Logs JSON                           → ese caso concreto: ¿qué pasó exactamente?
   ↓
pytest + k6                         → verifican corrección y permiten medir capacidad
```

Dos encadenamientos merecen destacarse porque son los que sostienen el diseño:

- **La idempotencia es lo que hace aceptable el 503.** Sin ella, rechazar rápido sería
  trasladar el problema al cliente; con ella, "reintente con la misma clave" es una
  instrucción segura. Rechazo rápido + idempotencia forman una sola decisión.
- **El outbox es lo que hace posible el desacople sin perder datos.** Sin él, sacar la IA
  del camino crítico habría significado aceptar eventos perdidos.

---

## 9.14 Tabla resumen

| Decisión | Rendimiento | Seguridad / Integridad | Escalabilidad | Trade-off |
|---|---|---|---|---|
| **FastAPI** | ASGI de bajo overhead; endpoints síncronos en threadpool, coherentes con un driver bloqueante | Validación Pydantic en el borde: lo inválido no abre transacción ni consume conexión | Servicios *stateless*, aptos para varios procesos e instancias | No escala por sí mismo: el límite sigue siendo la base de datos |
| **Python** | El trabajo pesado está en PostgreSQL, no en el intérprete | `Decimal` explícito, SQL parametrizado, esquemas tipados | Escala por procesos, no por hilos, por el GIL | Menor rendimiento CPU-bound que Go/Java/Rust |
| **PostgreSQL** | OLTP con MVCC, bloqueo por fila e índices ajustados al caso | ACID, `CHECK`, `UNIQUE`, FKs, triggers de inmutabilidad, rol sin `DELETE` | Vertical amplio; réplicas de lectura como evolución | Es el límite central: nodo único, `max_connections` y contención |
| **psycopg2 sin ORM** | Ocho sentencias por transferencia, con `RETURNING` y medición por operación | SQL parametrizado; bloqueos y `commit`/`rollback` explícitos | Permite controlar el pool y evitar indirección en la ruta caliente | Más código manual y menor portabilidad entre motores |
| **`Decimal` + `NUMERIC(18,2)`** | Algo más lento que `float`, despreciable frente a la E/S | Exactitud monetaria: sin errores de representación binaria | Sin impacto relevante a este volumen | Coste aritmético mayor que entero o binario |
| **`UPDATE` condicionado + `FOR NO KEY UPDATE` en el fallback** | El caso feliz no hace bloqueo previo; menos viajes y bloqueos más cortos | Elimina la ventana leer-modificar-escribir; impide el doble gasto | Correcto con cualquier número de instancias: arbitra la fila | Cuentas calientes se serializan: consistencia sobre throughput |
| **Orden determinista por UUID** | Sin coste adicional | Reduce el riesgo de deadlock en transferencias cruzadas | Aplica igual con más concurrencia | No elimina deadlocks; obliga a savepoint cuando el destino va primero |
| **Idempotencia (clave + SHA-256 + `UNIQUE`)** | Sin consulta previa: solo los duplicados pagan la relectura | Impide duplicar dinero; hace segura la recuperación ante incidentes | Vive en la base compartida: vale para todos los workers | Columna y lógica adicionales; el cliente gestiona sus claves |
| **Pool con cola acotada** | Evita abrir conexión por petición; instrumentado en cuatro puntos | Acota conexiones; devuelve conexiones limpias o las descarta | Debe dimensionarse con workers y `max_connections` | Pequeño → espera y 503; grande → presión sobre PostgreSQL |
| **Transactional Outbox** | El commit no habla con el broker: solo un `INSERT` local | Si la transferencia está confirmada, su evento existe | Publicación desacoplada; `SKIP LOCKED` admite varios workers | Consistencia eventual, *at-least-once* y un proceso más |
| **RabbitMQ** | Saca de la petición el trabajo no crítico; absorbe picos | Cola durable y `ack` manual; la caída de IA no afecta al dinero | Varios consumidores sin tocar el productor | Operación adicional, backlog y, hoy, **sin DLQ** |
| **IA desacoplada** | La transferencia no espera a la inferencia | Ninguna ruta de error de IA alcanza el endpoint financiero | Consumidores escalables aparte de la API | Recomendación eventual; más componentes |
| **Pandas (ETL)** | Suficiente para el volumen actual; sin coste de arranque de un clúster | Validación y separación de registros observados con su motivo | Limitado por memoria: sin streaming | No sirve para volúmenes que no quepan en memoria |
| **Docker Compose** | No es una decisión de rendimiento | Aislamiento, red interna y configuración por entorno | Base para orquestación posterior | No es orquestación productiva; `container_name` impide `--scale` |
| **Logs JSON** | Un log por operación relevante, no por paso | No se registran importes ni números de cuenta | Listos para centralización | Formato no homogéneo entre servicios |
| **Prometheus** | Recolección por *pull* cada 5 s, sin agentes | Cardinalidad acotada: sin IDs como etiquetas | Suficiente para el alcance; evolucionable a remoto | Alta cardinalidad es su punto débil; `/metrics` sin protección |
| **Grafana** | Consulta y visualiza; no recolecta | Debería requerir autenticación en producción | Dashboards aprovisionados por archivo, versionados | Hoy no cubre pool, threadpool ni SQL por operación |
| **OpenTelemetry** | Exportación por lotes; muestreo posible en producción | Sin importes ni cuentas en los atributos de span | Estándar neutral: el backend es intercambiable | Coste de instrumentación; dos identificadores de correlación conviven |
| **Jaeger** | Localiza el span que consume el tiempo | Visibilidad sin exponer datos de negocio | Adecuado al alcance del MVP | *All-in-one* guarda en memoria: las trazas se pierden al reiniciar |
| **k6** | Mide latencia, throughput y errores a tasa de llegada constante | Datasets sin credenciales ni datos personales | Ejecutable distribuido si hiciera falta | Generador local compite por CPU; **sin resultados versionados** |

---

## 9.15 Deuda técnica reconocida

1. **Código muerto** en `servicios/transferencias.py`: implementación asíncrona previa que
   referencia nombres inexistentes (`SessionLocal`, `DBAPIError`) de una librería que ni
   siquiera está instalada. Durante un incidente, código que parece activo y no lo es hace
   perder tiempo **[LIM]**.
2. **Definiciones duplicadas**: `CuentaNoEncontrada` y `ConflictoIdempotencia` en
   `transferencias.py`; `ConflictoIdempotencia` y `validar_idempotencia` en
   `idempotencia.py` **[LIM]**.
3. **Sin DLQ** en el consumidor de IA y **sin backoff** en los reintentos del outbox
   **[LIM]**.
4. **Eventos que quedan en `PROCESANDO`** si un worker muere: no hay recuperación automática
   **[LIM]**.
5. **Esquema aplicado solo en la inicialización del contenedor**, sin migraciones
   versionadas **[LIM]**.
6. **Sin autenticación** entre servicios ni hacia el exterior **[LIM]**.
7. **Métricas no agregadas** entre los workers de Uvicorn **[LIM]**.
8. **Dos identificadores distintos llamados `trace_id`** en los logs **[LIM]**.
9. **`ddl.sql` tiene nombres fijos** (`smartbanks_db`, `svc_banc_user`) mientras el resto de
   la configuración es parametrizable: cambiar esas variables rompería la inicialización
   **[LIM]**.

Ninguna afecta a la corrección del camino financiero, que es lo que las pruebas verifican;
todas afectan a la operación o al mantenimiento, que es donde deben corregirse.
