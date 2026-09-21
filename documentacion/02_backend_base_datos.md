# 2. Backend y base de datos

Este documento describe el núcleo transaccional de SmartBancs: la API de transferencias,
el esquema de base de datos, el comportamiento ACID, la idempotencia, la concurrencia, el
pool de conexiones y el manejo de errores, tal como están implementados en el
repositorio.

---

## 2.1 Estructura del servicio

`servicio_transacciones` está organizado en capas, sin ORM **[MVP]**:

| Capa | Archivos | Responsabilidad |
|---|---|---|
| Aplicación | [app/main.py](../servicio_transacciones/app/main.py) | Instancia FastAPI, ciclo de vida, middleware, montaje de `/metrics`, manejador de `PoolAgotado`, `GET /health`. |
| Rutas | [rutas/transferencias.py](../servicio_transacciones/app/rutas/transferencias.py), [rutas/cuentas.py](../servicio_transacciones/app/rutas/cuentas.py) | Contrato HTTP y traducción de excepciones de dominio a códigos de estado. |
| Esquemas | [esquemas/transacciones.py](../servicio_transacciones/app/esquemas/transacciones.py), [esquemas/cuentas.py](../servicio_transacciones/app/esquemas/cuentas.py) | Validación Pydantic de entrada y forma de la respuesta. |
| Servicios | [servicios/transferencias.py](../servicio_transacciones/app/servicios/transferencias.py), [servicios/idempotencia.py](../servicio_transacciones/app/servicios/idempotencia.py), [servicios/hash_solicitud.py](../servicio_transacciones/app/servicios/hash_solicitud.py), [servicios/cuentas.py](../servicio_transacciones/app/servicios/cuentas.py) | Lógica de negocio, control transaccional y reintentos. |
| Repositorios | [repositorios/transacciones.py](../servicio_transacciones/app/repositorios/transacciones.py), [repositorios/cuentas.py](../servicio_transacciones/app/repositorios/cuentas.py) | SQL parametrizado y mapeo de filas a diccionarios. |
| Base de datos | [database/conexion.py](../servicio_transacciones/app/database/conexion.py) | Pool de conexiones, préstamo con espera acotada y saneamiento al devolver. |
| Observabilidad | [observabilidad/](../servicio_transacciones/app/observabilidad/) | Métricas Prometheus, logging JSON, contexto de traza y OpenTelemetry. |
| Configuración | [app/configuracion.py](../servicio_transacciones/app/configuracion.py) | `Settings` de pydantic-settings leído de variables de entorno. |

Los endpoints de transferencia se declaran como funciones **síncronas** (`def`, no
`async def`). FastAPI las ejecuta en el threadpool de AnyIO, cuyo límite se fija en el
`lifespan` a `ANYIO_THREAD_TOKENS` (40 por defecto). Esto es coherente con psycopg2, que
es un driver bloqueante.

---

## 2.2 Endpoints

| Método y ruta | Propósito |
|---|---|
| `POST /transacciones` | Procesa una transferencia. Camino crítico. |
| `GET /cuentas` | Lista cuentas con campos seguros (`numero_cuenta`, `estado`, `divisa`), con búsqueda opcional, filtro `solo_activas` y `limite` entre 1 y 200. |
| `GET /cuentas/{numero_cuenta}/resolver` | Traduce número de cuenta a `id_cuenta` (UUID) y `estado`. Uso servicio a servicio. |
| `GET /health` | Estado del servicio. |
| `GET /metrics` | Exposición Prometheus (aplicación ASGI montada). |

`GET /cuentas` no devuelve saldo ni titular, y `GET /cuentas/{numero}/resolver` solo
devuelve identificador y estado: las consultas de lectura están escritas para no exponer
datos que la interfaz no necesita **[MVP]**
([repositorios/cuentas.py](../servicio_transacciones/app/repositorios/cuentas.py)).

---

## 2.3 Flujo real de `POST /transacciones`

El orden siguiente corresponde al código, no a un diseño ideal.

### 1. Recepción de la solicitud

El middleware de [main.py](../servicio_transacciones/app/main.py) marca el instante de
entrada (`smartbancs_request_inicio`), muestrea el limitador del threadpool de AnyIO,
toma la cabecera `X-Trace-Id` o genera un UUID si no llega, y la fija en un `ContextVar`.
La misma cabecera se devuelve en la respuesta.

### 2. Validación del esquema

`TransferenciaCreate` valida antes de que se ejecute la lógica **[MVP]**:

- `id_idempotencia`: cadena de 1 a 100 caracteres;
- `cuenta_origen_id` y `cuenta_destino_id`: UUID;
- `monto`: `Decimal` mayor que cero, máximo 18 dígitos y 2 decimales;
- `divisa`: exactamente 3 caracteres, normalizada a mayúsculas por un validador;
- validador cruzado: cuenta origen y destino deben ser distintas.

Un fallo aquí produce **422** y el middleware lo registra en
`smartbancs_http_errores_total` con `tipo="validacion"`.

### 3. Hash de solicitud

`generar_hash_solicitud` construye la cadena
`origen|destino|monto(2 decimales)|DIVISA` y devuelve su SHA-256
([hash_solicitud.py](../servicio_transacciones/app/servicios/hash_solicitud.py)). Ese
hash es la huella del cuerpo de la petición y se almacena junto a la transacción.

### 4. Obtención de conexión

Cada intento obtiene su propio préstamo del pool
(`contextmanager(obtener_conexion)()`). Si no hay conexión disponible dentro del plazo se
lanza `PoolAgotado`, que el manejador registrado en `main.py` traduce a **503** con
`Retry-After: 1`. Ver 2.7.

### 5. Inicio de transacción

`_procesar_transferencia` entra en `with _MedirFinTransaccion(conn)`, un envoltorio que
delega en el context manager de psycopg2 (`with conn`) y cronometra únicamente su salida.
Todo lo que sigue ocurre dentro de **una sola transacción**: `COMMIT` al salir sin error,
`ROLLBACK` si se propaga una excepción.

Antes de abrir el trabajo se inyecta el contexto de traza W3C en un diccionario que
viajará dentro de la carga útil del evento outbox.

### 6. Idempotencia

`obtener_o_crear_transaccion` consulta por `id_idempotencia`; si no existe, inserta la
fila bajo `SAVEPOINT sp_idempotencia`. Ver 2.4 para el detalle completo.

- Si la clave ya existía con el **mismo** hash, el resultado es `creada = False` y el
  flujo salta directamente al armado de la respuesta: **no se mueve dinero**.
- Si existía con **otro** hash, se lanza `ConflictoIdempotencia` → **409**.
- Si la inserción falla por `ForeignKeyViolation` (alguna cuenta no existe), se traduce a
  `CuentaNoEncontrada` → **404**.

### 7 y 8. Operación financiera y actualización de cuentas

Solo para transacciones nuevas:

1. `SAVEPOINT sp_transferencia_saldos`.
2. Las dos cuentas se procesan en el orden lexicográfico de sus UUID
   (`sorted((origen, destino))`), independientemente de cuál sea origen o destino.
3. Para la cuenta origen se ejecuta `debitar_cuenta`:
   `UPDATE cuentas SET saldo = saldo - %s WHERE id_cuenta = %s AND estado = 'ACTIVA' AND divisa = %s AND saldo >= %s RETURNING saldo`.
4. Para la cuenta destino, `acreditar_cuenta`: mismo patrón sin la condición de saldo.
5. Si **cualquiera** de los dos `UPDATE` no devuelve fila (`RETURNING` vacío), la función
   devuelve `None` y se activa la ruta de clasificación.

Este es el punto clave del diseño: en el camino normal **no hay `SELECT ... FOR UPDATE`**.
El propio `UPDATE` condicionado hace de validación y de bloqueo de fila.

### Ruta de rechazo y clasificación

Cuando un `UPDATE` no afecta filas no se sabe todavía *por qué*. El código entonces:

1. `ROLLBACK TO SAVEPOINT sp_transferencia_saldos` y `RELEASE` — esto también deshace un
   crédito ya aplicado cuando el destino tenía el UUID menor y se procesó primero;
2. ejecuta `obtener_cuentas_para_actualizar`:
   `SELECT id_cuenta, saldo, divisa, estado FROM cuentas WHERE id_cuenta IN (%s, %s) ORDER BY id_cuenta FOR NO KEY UPDATE`;
3. llama a `_clasificar_rechazo`, que devuelve el motivo:
   `CUENTA_ORIGEN_NO_ACTIVA`, `CUENTA_DESTINO_NO_ACTIVA`, `DIVISA_NO_COMPATIBLE` o
   `SALDO_INSUFICIENTE`; si falta alguna de las dos filas lanza `CuentaNoEncontrada`;
4. si la clasificación devuelve `None` —el estado cambió entre el `UPDATE` fallido y la
   lectura— repite la actualización con las filas ya bloqueadas; si aun así falla, lanza
   `RuntimeError("Las cuentas cambiaron estando bloqueadas")`.

El bloqueo explícito es, por tanto, **el camino de excepción**, no el habitual.

### 9. Movimientos

Solo si no hay motivo de rechazo se insertan **dos** movimientos con
`insertar_movimiento`: uno `DEBITO` sobre la cuenta origen y uno `CREDITO` sobre la
destino, cada uno con el `saldo_resultante` devuelto por el `RETURNING` del `UPDATE`
correspondiente. Ver 2.10.

### 10. Actualización del estado de la transacción

- Con motivo de rechazo: `rechazar_transaccion` deja `estado = 'RECHAZADA'` y
  `razon_fallo`.
- Sin motivo: `completar_transaccion` deja `estado = 'COMPLETADA'` y
  `completado_en = CURRENT_TIMESTAMP`.

### 11. Inserción en `eventos_outbox`

Solo en el caso `COMPLETADA` se inserta el evento `TRANSFERENCIA_COMPLETADA` con
`insertar_evento_outbox`. La carga útil (`jsonb`) incluye `trace_id`, `trace_context`,
`id_transaccion`, ambas cuentas, `monto`, `divisa` y `estado`.

Una transferencia **rechazada no genera evento** **[LIM]**.

### 12. COMMIT

Al salir del bloque `with` se confirma la transacción y se liberan los bloqueos. A partir
de aquí **no se ejecuta más SQL**: la respuesta se arma con lo que ya devolvieron los
`RETURNING`, de modo que la conexión vuelve al pool sin abrir otra transacción.

### 13. Respuesta HTTP

`201 Created` con el cuerpo de `TransferenciaResponse`. Nota importante del contrato: una
transferencia **rechazada por reglas de negocio también responde 201**, con
`estado = "RECHAZADA"` y `razon_fallo` explicando el motivo. El rechazo de negocio no es
un error HTTP **[MVP]**.

```text
                       ┌───────── 201 estado=COMPLETADA
POST /transacciones ───┼───────── 201 estado=RECHAZADA (razon_fallo)
                       ├───────── 201 reproducida=true (idempotencia)
                       ├───────── 404 cuenta inexistente
                       ├───────── 409 misma clave, distinto payload
                       ├───────── 422 validación de esquema
                       ├───────── 500 error de PostgreSQL no reintentable
                       └───────── 503 pool agotado / saturación tras reintentos
```

---

## 2.4 Idempotencia

**Para qué sirve.** Una transferencia es una operación no repetible. Si el cliente
reintenta por timeout o corte de red, el sistema debe reconocer que es la misma
operación y no cobrarla dos veces.

**Cómo se identifica una solicitud.** Por `id_idempotencia`, una cadena que envía el
cliente. `demo_web` genera un UUID si el frontend no la aporta, y no la expone al
navegador.

**Papel del hash.** `hash_solicitud` (SHA-256 de origen, destino, monto y divisa) permite
distinguir un reintento legítimo de una reutilización incorrecta de la clave. La columna
es `char(64)`, de ahí que el repositorio aplique `.strip()` al leerla.

**Mecanismos utilizados [MVP]:**

- **`UNIQUE`**: `uq_transacciones_idempotencia unique (id_idempotencia)` en el DDL. Es el
  árbitro final bajo concurrencia.
- **`SAVEPOINT`**: el `INSERT` se hace entre `SAVEPOINT sp_idempotencia` y
  `RELEASE SAVEPOINT`. Si otra petición gana la carrera, la `UniqueViolation` se captura,
  se hace `ROLLBACK TO SAVEPOINT sp_idempotencia` y **la transacción principal sigue
  viva**; sin el savepoint, PostgreSQL habría abortado toda la transacción.

**Casos:**

| Situación | Comportamiento | Respuesta |
|---|---|---|
| Clave nueva | Se inserta y se procesa la transferencia. | `201`, `reproducida = false` |
| Misma clave, mismo hash | Se devuelve la transacción ya almacenada sin tocar saldos. | `201`, `reproducida = true` |
| Misma clave, hash distinto | `validar_idempotencia` lanza `ConflictoIdempotencia`. | `409` |
| Dos peticiones concurrentes con la misma clave | Una inserta; la otra recibe `UniqueViolation`, vuelve al savepoint, relee la fila ganadora y la devuelve. | `201` para ambas, una con `reproducida = true` |

**Decisión de diseño.** No hay consulta previa de idempotencia fuera de la transacción: el
`SELECT` inicial y el `INSERT` ocurren dentro de la misma transacción que el resto de la
operación, y la restricción `UNIQUE` resuelve el empate. Así el caso mayoritario (clave
nueva) no paga viajes adicionales.

**Límite.** La respuesta reproducida se construye con la fila almacenada; si la primera
petición dejó la transacción en `PENDIENTE` por un fallo posterior, la reproducción
devolvería ese estado **[LIM]**. En el flujo actual todo ocurre en la misma transacción,
por lo que `PENDIENTE` no sobrevive a un `COMMIT` exitoso.

---

## 2.5 Base de datos

Las tablas se crean en [base_datos/ddl.sql](../base_datos/ddl.sql). Los tipos enumerados
son `estados_cuenta`, `estados_transaccion`, `estados_outbox` y `tipos_movimiento`.

### `cuentas`

Responsabilidad: saldo y estado de cada cuenta.

| Elemento | Detalle |
|---|---|
| Clave primaria | `id_cuenta UUID` con `gen_random_uuid()` (extensión `pgcrypto`) |
| Clave natural | `numero_cuenta varchar(30) NOT NULL UNIQUE` |
| Columnas | `nombre_titular`, `saldo numeric(18,2)`, `divisa varchar(3)`, `estado estados_cuenta`, `creado_en`, `actualizado_en` |
| Constraints | `chk_cuentas_saldo_no_negativo (saldo >= 0)`; formato de divisa de tres mayúsculas |
| Estados | `ACTIVA`, `BLOQUEADA`, `CERRADA` |
| Trigger | `trg_cuentas_actualizado_en` actualiza `actualizado_en` en cada `UPDATE` |

El `CHECK` de saldo no negativo es la última línea de defensa contra el sobregiro,
independiente del `WHERE saldo >= monto` de la aplicación.

### `transacciones`

Responsabilidad: registro de la intención y el resultado de cada transferencia.

| Elemento | Detalle |
|---|---|
| Clave primaria | `id_transaccion UUID` |
| Unicidad | `uq_transacciones_idempotencia (id_idempotencia)` |
| Relaciones | `fk_transacciones_cuenta_origen` y `fk_transacciones_cuenta_destino` hacia `cuentas(id_cuenta)` |
| Columnas | `hash_solicitud char(64)`, `monto numeric(18,2)`, `divisa`, `estado estados_transaccion`, `razon_fallo varchar(255)`, `creado_en`, `completado_en` |
| Constraints | `monto > 0`; cuentas distintas; formato de divisa |
| Estados | `PENDIENTE` (valor por defecto), `COMPLETADA`, `RECHAZADA` |
| Índices | `idx_transacciones_cuenta_origen (cuenta_origen_id, creado_en)`, `idx_transacciones_cuenta_destino (cuenta_destino_id, creado_en)` |

Triggers de integridad **[MVP]**:

- `trg_transacciones_no_delete`: prohíbe `DELETE`.
- `trg_transacciones_update_controlado`: los datos financieros originales
  (`id_idempotencia`, `hash_solicitud`, cuentas, `monto`, `divisa`, `creado_en`) son
  inmutables; `COMPLETADA` y `RECHAZADA` son estados finales; desde `PENDIENTE` solo se
  admiten transiciones a `COMPLETADA` o `RECHAZADA`; una transacción `COMPLETADA` debe
  tener `completado_en`.

### `movimientos`

Responsabilidad: asiento de cada afectación de saldo.

| Elemento | Detalle |
|---|---|
| Clave primaria | `id_movimiento UUID` |
| Relaciones | `transaccion_id` → `transacciones`, `cuenta_id` → `cuentas` |
| Columnas | `tipo tipos_movimiento` (`DEBITO` / `CREDITO`), `monto`, `saldo_resultante`, `creado_en` |
| Constraints | `monto > 0`; `uq_movimientos_tx_tipo unique (transaccion_id, tipo)` |
| Índice | `idx_movimientos_cuenta_fecha (cuenta_id, creado_en)` |
| Trigger | `trg_movimientos_inmutables` bloquea `UPDATE` y `DELETE` |

La unicidad `(transaccion_id, tipo)` impide registrar dos débitos o dos créditos para la
misma transferencia.

### `eventos_outbox`

Responsabilidad: intención de publicar un evento, escrita en la misma transacción
financiera.

| Elemento | Detalle |
|---|---|
| Clave primaria | `id_eventos UUID` |
| Relación | `transaccion_id` → `transacciones` |
| Columnas | `tipo_evento varchar(100)`, `carga_util jsonb`, `estado estados_outbox`, `intentos integer` (con `CHECK >= 0`), `creado_en`, `publicado_en` |
| Estados | `PENDIENTE`, `PROCESANDO`, `PUBLICADO`, `FALLIDO` |
| Índice | `idx_eventos_outbox_pendientes on eventos_outbox(creado_en) where estado = 'PENDIENTE'` (índice parcial, dimensionado para la consulta del worker) |

### `analisis_ia`

Responsabilidad: resultado del análisis asíncrono. Se documenta aquí solo por su relación
con `transacciones`; el detalle está en
[04_inteligencia_artificial.md](04_inteligencia_artificial.md).

`transaccion_id` es `UNIQUE` y tiene clave foránea hacia `transacciones`, lo que permite
que la persistencia del consumidor sea un *upsert* idempotente.

---

## 2.6 DDL, DML y seed de carga

Tres scripts con propósitos distintos **[MVP]**:

| Script | Tipo | Contenido | Cuándo se ejecuta |
|---|---|---|---|
| [base_datos/init_user.sh](../base_datos/init_user.sh) | Inicialización | Crea el rol de aplicación si no existe, con nombre y contraseña de `DB_APP_USER` / `DB_APP_PASSWORD`. | Automático, `01_init_user.sh` |
| [base_datos/ddl.sql](../base_datos/ddl.sql) | **DDL** | Extensiones (`pgcrypto`, `pg_stat_statements`), 4 tipos enumerados, 5 tablas, constraints, 4 índices, funciones y triggers de inmutabilidad, `GRANT` al rol de aplicación y límites de tiempo del rol. | Automático, `02_ddl.sql` |
| [base_datos/dml.sql](../base_datos/dml.sql) | **DML** | 25 cuentas de demostración `SB-000006` … `SB-000030`, con saldos variados, casos límite (0.01, 250000.00) y estados no activos (`CERRADA`, `BLOQUEADA`). | Automático, `03_dml.sql` |
| [base_datos/cuentas_carga.sql](../base_datos/cuentas_carga.sql) | **Seed de carga** | 5.000 cuentas `LOAD-000001` … `LOAD-005000` con saldo 100000.00, generadas con `generate_series` y `ON CONFLICT (numero_cuenta) DO NOTHING` (re-ejecutable). | **Manual**: no está montado en `docker-entrypoint-initdb.d` |

Los tres primeros solo corren en la **primera** inicialización del volumen
`postgres_data`. El seed de carga es para las pruebas de rendimiento y se ejecuta a mano;
su uso se documenta en [08_pruebas_rendimiento.md](08_pruebas_rendimiento.md).

El rol de aplicación recibe `CONNECT`, `USAGE` sobre el esquema y `SELECT, INSERT, UPDATE`
sobre las tablas (también por `ALTER DEFAULT PRIVILEGES`). **No recibe `DELETE`** **[MVP]**.

---

## 2.7 Pool de conexiones y timeouts

### Pool

`servicio_transacciones` usa `ThreadedConnectionPool` de psycopg2 envuelto en la clase
`PoolConEspera` ([database/conexion.py](../servicio_transacciones/app/database/conexion.py)).
No hay pool de SQLAlchemy ni ORM.

| Parámetro | Origen | Valor en `docker-compose.yml` | Valor por defecto en código |
|---|---|---|---|
| `DB_POOL_SIZE` | entorno | 15 | 15 |
| `DB_MAX_OVERFLOW` | entorno | 0 | 0 |
| `DB_POOL_MIN_CONN` | entorno | 15 | 15 |
| `DB_POOL_TIMEOUT_S` | entorno | 0.05 | 0.05 |
| `ANYIO_THREAD_TOKENS` | entorno | 40 (`${ANYIO_THREAD_TOKENS:-40}`) | 40 |
| `UVICORN_WORKERS` | entorno | 5 (`${UVICORN_WORKERS:-5}`) | 5 en el `CMD` del Dockerfile |

Comportamiento **[MVP]**:

- El pool se crea de forma perezosa y **por proceso**: se guarda el PID y se recrea si
  cambia, porque cada worker de Uvicorn es un proceso independiente. Las conexiones
  totales son `workers × (pool_size + max_overflow)`, es decir 5 × 15 = 75 frente a
  `max_connections=200` de PostgreSQL.
- `PoolConEspera` limita los préstamos con un `BoundedSemaphore` de capacidad
  `pool_size + max_overflow` y una **cola corta** adicional de `min(10, capacidad)`.
- Si no hay slot libre: si la cola está llena, rechazo inmediato
  (`motivo="cola_llena"`); si hay sitio en la cola, se espera como máximo
  `DB_POOL_TIMEOUT_S` y, si vence, rechazo (`motivo="timeout"`). Un error del pool
  interno da `motivo="pool_error"`. Los tres lanzan `PoolAgotado` → **503**.
- Las conexiones se abren con `application_name="smartbancs"`, visible en
  `pg_stat_activity` y en el prefijo de log de PostgreSQL.

Tensión deliberada: el threadpool admite hasta 40 peticiones síncronas simultáneas por
worker, pero solo 15 pueden sostener una conexión. El exceso espera 50 ms y se rechaza con
503 en lugar de acumular una cola que ya no llegaría a tiempo.

### Devolución y conexiones rotas

`obtener_conexion` es un generador que siempre devuelve la conexión al pool **[MVP]**:

- Si la conexión está cerrada, su estado transaccional es desconocido, o el error indica
  que el servidor cerró la sesión (`InterfaceError`, SQLSTATE `08*`, `25P03`, `57P01`,
  `57P02`, `57P03`), se descarta con `putconn(close=True)`.
- En caso normal se hace `rollback()` antes de devolverla, para no entregar nunca al
  siguiente request una transacción abierta o abortada. Si ese `rollback` falla, la
  conexión también se descarta.
- `error_de_conexion_perdida` excluye explícitamente `QueryCanceled` (57014),
  `LockNotAvailable` (55P03) y `TransactionRollbackError` (40P01): son conflictos de la
  sentencia, no de la conexión, así que la conexión se reutiliza.

### Timeouts

| Nivel | Valor | Dónde se define |
|---|---|---|
| Espera de préstamo del pool | 0,05 s | `DB_POOL_TIMEOUT_S` (entorno) |
| `lock_timeout` | 800 ms | `ALTER ROLE svc_banc_user` en `ddl.sql` |
| `statement_timeout` | 1500 ms | `ALTER ROLE svc_banc_user` en `ddl.sql` |
| `idle_in_transaction_session_timeout` | 5 s | `ALTER ROLE svc_banc_user` en `ddl.sql` |
| `deadlock_timeout` (detección del servidor) | 200 ms | `command` del contenedor `postgres` |

Definir los tres primeros **en el rol** y no en la aplicación hace que apliquen a toda
conexión del servicio aunque el código cambie, y convierte una espera indefinida en un
error con SQLSTATE conocido que el servicio ya sabe clasificar.

---

## 2.8 ACID en SmartBancs

### Atomicidad

Dentro de la misma transacción ocurren: el `INSERT` en `transacciones`, los dos `UPDATE`
de saldo, los dos `INSERT` en `movimientos`, el `UPDATE` de estado de la transacción y el
`INSERT` en `eventos_outbox`. O se confirma todo, o no queda nada **[MVP]**.

Dentro de la transacción hay dos savepoints con propósitos distintos:
`sp_idempotencia` (absorbe la `UniqueViolation` sin abortar la transacción) y
`sp_transferencia_saldos` (deshace un crédito ya aplicado cuando el débito falla, sin
perder la fila de transacción ya creada).

Si se propaga cualquier excepción, el bloque `with` revierte y además se llama
explícitamente a `conn.rollback()` cuando la conexión sigue abierta, dejándola reutilizable
para el siguiente intento.

### Consistencia

No depende solo del código de aplicación:

| Regla | Dónde se garantiza |
|---|---|
| El saldo nunca queda negativo | `CHECK (saldo >= 0)` + `WHERE saldo >= %s` en el `UPDATE` |
| El monto siempre es positivo | `CHECK (monto > 0)` en `transacciones` y `movimientos` + validación Pydantic |
| Origen y destino distintos | `CHECK` en la tabla + validador Pydantic |
| Una clave de idempotencia, una transacción | `UNIQUE (id_idempotencia)` |
| Dos movimientos por transferencia, uno de cada tipo | `UNIQUE (transaccion_id, tipo)` |
| Las cuentas referenciadas existen | Claves foráneas (su violación se traduce a 404) |
| Los estados finales no se revierten | Trigger `trg_transacciones_update_controlado` |
| El libro de movimientos no se altera | Trigger `trg_movimientos_inmutables` + ausencia de `DELETE` en los permisos del rol |

### Aislamiento

El nivel de aislamiento es el **`READ COMMITTED` por defecto de PostgreSQL**: el proyecto
no lo cambia en ningún punto **[MVP]**. El aislamiento efectivo del saldo no proviene del
nivel, sino de los bloqueos de fila que toman los propios `UPDATE`:

1. **Orden determinista.** Las cuentas se procesan ordenadas por UUID, de modo que todas
   las transferencias adquieren los bloqueos de fila en el mismo orden.
2. **`UPDATE` condicionado.** En el camino normal **no se usa `SELECT ... FOR UPDATE`**.
   El `UPDATE ... WHERE saldo >= monto AND estado = 'ACTIVA' AND divisa = %s` bloquea la
   fila y comprueba la condición sobre la versión ya confirmada, en una sola operación
   atómica. Dos transferencias sobre la misma cuenta se serializan en esa fila.
3. **Savepoint y reversión parcial.** Si el `UPDATE` no afecta filas, se vuelve al
   savepoint para deshacer lo aplicado.
4. **Bloqueo explícito solo como fallback.** `SELECT ... ORDER BY id_cuenta FOR NO KEY UPDATE`
   se ejecuta únicamente en la ruta de clasificación del rechazo. Se usa `FOR NO KEY UPDATE`
   y no `FOR UPDATE` porque no se modifican columnas de clave, lo que permite mayor
   concurrencia con las claves foráneas que apuntan a `cuentas`.
5. **Reclasificación defensiva.** Si tras bloquear las filas resulta que ya no hay motivo
   de rechazo (el estado cambió entre medias), la operación se repite con las filas
   bloqueadas.

### Durabilidad

Al confirmar, PostgreSQL persiste la transacción según su configuración por defecto de
WAL; el proyecto **no modifica** `synchronous_commit`, `fsync` ni ningún parámetro de WAL,
por lo que aplican los valores por defecto de la imagen `postgres:16-alpine` **[MVP]**.
Los datos se almacenan en el volumen Docker `postgres_data`.

**[LIM]**: un único nodo, sin réplica ni copia de seguridad configurada.
**[PROD]**: replicación y política de respaldo.

---

## 2.9 Concurrencia y race conditions

| Riesgo | Mecanismo real en el MVP |
|---|---|
| **Doble gasto** (dos transferencias simultáneas sobre el mismo saldo) | El `UPDATE` con `saldo >= monto` bloquea la fila; la segunda transacción espera y reevalúa la condición sobre el saldo ya descontado. Si no alcanza, no afecta filas y se rechaza con `SALDO_INSUFICIENTE`. El `CHECK (saldo >= 0)` respalda la regla. |
| **Actualización perdida** (*lost update*) | No hay patrón leer-modificar-escribir en la aplicación: el saldo se calcula en SQL (`saldo = saldo - %s`) sobre la fila bloqueada. |
| **Transacción duplicada** | `UNIQUE (id_idempotencia)` más el `SAVEPOINT` que captura la `UniqueViolation`. |
| **Movimientos duplicados** | `UNIQUE (transaccion_id, tipo)`. |
| **Crédito huérfano** (el débito falla después de acreditar al destino) | `SAVEPOINT sp_transferencia_saldos` y `ROLLBACK TO SAVEPOINT`, relevante cuando el UUID del destino es menor y se procesa primero. |
| **Estado que cambia durante la clasificación** | Reclasificación con las filas ya bloqueadas por `FOR NO KEY UPDATE`. |
| **Saturación** (muchas peticiones compitiendo por conexiones) | Semáforo del pool con cola corta y rechazo rápido con 503. |

Protecciones en la aplicación: orden determinista, savepoints, reintentos acotados,
límite del threadpool y del pool de conexiones.
Protecciones en PostgreSQL: bloqueos de fila, `UNIQUE`, `CHECK`, claves foráneas,
triggers, detección de deadlock y timeouts de rol.

---

## 2.10 Movimientos financieros

Cada transferencia **completada** produce exactamente **dos** filas en `movimientos`:
un `DEBITO` sobre la cuenta origen y un `CREDITO` sobre la destino, ambas con el mismo
`transaccion_id`, el mismo `monto` y el `saldo_resultante` devuelto por el `RETURNING` del
`UPDATE` correspondiente **[MVP]**.

Esto es un registro de doble asiento **lógico** para el caso concreto de la transferencia,
no un libro mayor contable completo: no existen cuentas contables, ni asientos de
contrapartida, ni un plan de cuentas. La afirmación defendible es: *dos movimientos por
transferencia, inmutables y trazables a su transacción*.

Trazabilidad: desde un movimiento se llega a la transacción (`transaccion_id`) y a la
cuenta (`cuenta_id`); el índice `idx_movimientos_cuenta_fecha` soporta la consulta por
cuenta y fecha. Las transferencias **rechazadas no generan movimientos**.

---

## 2.11 Transactional Outbox desde el backend

**Problema: doble escritura (*dual write*).** La secuencia incorrecta sería:

```text
1. COMMIT de la operación financiera
2. publicar en RabbitMQ
3. la publicación falla (broker caído, red, proceso muerto)
   → transacción confirmada, evento perdido, IA nunca se entera
```

Invertir el orden tampoco sirve: se publicaría un evento de una transferencia que después
podría no confirmarse.

**Solución implementada [MVP]:**

```text
1. operación financiera (saldos, movimientos, estado)
2. INSERT en eventos_outbox  ← misma transacción
3. COMMIT único
4. worker_outbox reclama eventos PENDIENTE
5. worker_outbox publica en RabbitMQ y marca PUBLICADO
```

Nombres reales verificados:

| Elemento | Nombre real |
|---|---|
| Tabla | `eventos_outbox` |
| Estados | `PENDIENTE`, `PROCESANDO`, `PUBLICADO`, `FALLIDO` |
| Tipo de evento emitido | `TRANSFERENCIA_COMPLETADA` |
| Escritura desde el backend | `insertar_evento_outbox` en [repositorios/transacciones.py](../servicio_transacciones/app/repositorios/transacciones.py) |
| Proceso lector | `worker_outbox` ([app/main.py](../worker_outbox/app/main.py)) |
| Reclamo | `reclamar_eventos_pendientes` con `FOR UPDATE SKIP LOCKED`, lote de 20 |
| Marcado | `marcar_evento_publicado`, `devolver_evento_a_pendiente`, `marcar_evento_fallido` |
| Reintentos | `MAX_INTENTOS_OUTBOX = 3` en [servicios/outbox.py](../worker_outbox/app/servicios/outbox.py) |

Desde el punto de vista del backend, la garantía es: **si la transferencia está
confirmada, su evento existe**. La entrega efectiva al consumidor es *at-least-once* y su
detalle pertenece a [01_arquitectura.md](01_arquitectura.md).

---

## 2.12 Camino crítico

Forman parte de la respuesta síncrona **[MVP]**:

- `demo_web` (solo cuando la petición viene de la interfaz de demostración);
- `servicio_transacciones`;
- PostgreSQL.

**No** forman parte de la respuesta: `worker_outbox`, RabbitMQ, `consumidor_ia`,
`servicio_ia` y `servicio_bancs`. La transferencia **no espera al análisis de IA**: el
cliente recibe su `201` tras el `COMMIT`, y la interfaz consulta el análisis después
(mientras no exista, la respuesta es `PENDIENTE`, no un error).

Caída del broker o del consumidor de IA: las transferencias siguen procesándose y los
eventos se acumulan en `eventos_outbox` como `PENDIENTE` **[MVP]**.

---

## 2.13 Manejo de errores

| Caso | Detección | Estado de la transacción | Respuesta | Métrica / log |
|---|---|---|---|---|
| Cuenta inexistente | `ForeignKeyViolation` al insertar, o filas ausentes en `_clasificar_rechazo` → `CuentaNoEncontrada` | `ROLLBACK`, no se crea nada | **404** | `smartbancs_http_errores_total{status="404",tipo="negocio",motivo="cuenta_no_encontrada"}` |
| Cuenta origen o destino no activa | `UPDATE` sin filas → clasificación | `RECHAZADA` (`CUENTA_ORIGEN_NO_ACTIVA` / `CUENTA_DESTINO_NO_ACTIVA`), **COMMIT** | **201** | `smartbancs_transacciones_total{estado="RECHAZADA"}`, log `transferencia_rechazada` |
| Saldo insuficiente | `UPDATE` sin filas → clasificación | `RECHAZADA` (`SALDO_INSUFICIENTE`), **COMMIT** | **201** | igual que el anterior |
| Divisa incompatible | `UPDATE` sin filas → clasificación | `RECHAZADA` (`DIVISA_NO_COMPATIBLE`), **COMMIT** | **201** | igual que el anterior |
| Idempotencia conflictiva | `validar_idempotencia` compara el hash | `ROLLBACK` del intento; la transacción original permanece intacta | **409** | `...{status="409",tipo="negocio",motivo="conflicto_idempotencia"}` |
| Deadlock (`40P01`) | `obtener_sqlstate` sobre `PsycopgError` | `ROLLBACK` completo; se reintenta hasta 3 intentos con backoff | **503** si se agotan, con `Retry-After: 1` | `smartbancs_db_deadlocks_total`, `smartbancs_transacciones_errores_total{tipo="deadlock"}`, `smartbancs_db_reintentos_total{sqlstate}`, log `db_deadlock` |
| Timeout de sentencia (`57014`) o de bloqueo (`55P03`) | igual | igual | **503** | `smartbancs_db_timeouts_total`, `...errores_total{tipo="timeout"}`, log `db_timeout` |
| Pool agotado | `PoolAgotado` con `motivo` | No llegó a abrirse transacción | **503** + `Retry-After: 1` | `smartbancs_db_pool_agotado_total`, `smartbancs_db_pool_rechazos_total{motivo}`, `smartbancs_db_pool_timeouts_total` |
| Otro error de PostgreSQL | SQLSTATE fuera del conjunto reintentable | `ROLLBACK`, **sin reintento** | **500** | `...errores_total{tipo="db_error"}`, `smartbancs_http_errores_total{status="500",tipo="db_error"}`, log `db_error` |
| Validación de esquema | Pydantic | No se ejecuta lógica | **422** | `...{status="422",tipo="validacion"}` |
| Excepción no controlada | Middleware | `ROLLBACK` | **500** | `...{status="500",tipo="interno",motivo="excepcion_no_controlada"}` |

Detalles del tratamiento **[MVP]**:

- Los reintentos solo cubren `40P01`, `55P03` y `57014`, los tres SQLSTATE en los que
  PostgreSQL ya revirtió la transacción completa y repetirla es seguro. El resto se
  propaga sin reintentar.
- El backoff entre intentos es `0.025 × 2^(intento-1)` segundos multiplicado por un factor
  aleatorio entre 0,5 y 1,5 (jitter), para evitar que varios clientes reintenten a la vez.
- El mensaje del 503 indica explícitamente reintentar **con la misma clave de
  idempotencia**; eso es lo que hace seguro el reintento del cliente.
- Los logs son JSON estructurado
  ([observabilidad/logging.py](../servicio_transacciones/app/observabilidad/logging.py))
  con campos `evento`, `trace_id`, `id_transaccion`, `sqlstate`, `motivo`, `estado`,
  `duracion_ms` y `tipo_error`.
- La etiqueta `motivo` de los 503 por saturación se restringe a `57014`, `55P03`, `40P01`
  u `otro`, para acotar la cardinalidad de la métrica.

---

## 2.14 Seguridad e integridad

Mecanismos realmente presentes **[MVP]**:

- **SQL siempre parametrizado**: todas las consultas usan marcadores de psycopg2
  (`%s` o parámetros con nombre); no hay concatenación de cadenas en el SQL.
- **Comodines de `LIKE` escapados**: la búsqueda de cuentas escapa `\`, `%` y `_` antes de
  construir el patrón, de modo que se traten como texto literal
  ([servicios/cuentas.py](../servicio_transacciones/app/servicios/cuentas.py)).
- **Validación en el borde**: Pydantic rechaza montos no positivos, divisas mal formadas,
  UUID inválidos y transferencias a la misma cuenta antes de tocar la base.
- **Usuarios de base de datos separados**: el superusuario de la imagen
  (`POSTGRES_USER`) solo se usa en la inicialización; los servicios se conectan con
  `DB_USER` (`svc_banc_user`), con permisos acotados y sin `DELETE`.
- **Límites de recurso en el rol**: `lock_timeout`, `statement_timeout` e
  `idle_in_transaction_session_timeout` acotan lo que una sola sesión puede retener.
- **Integridad en la base**: constraints y triggers de inmutabilidad (2.5).
- **No exposición de identificadores internos**: `demo_web` traduce número de cuenta a
  UUID y filtra `cuenta_origen_id`, `cuenta_destino_id` e `id_idempotencia` de la
  respuesta antes de devolverla al navegador.
- **Configuración por variables de entorno**: `.env` con `.env.example` versionado; el
  `.env` real no se versiona.

**[LIM]**: no existe autenticación ni autorización en ningún endpoint; cualquiera con
acceso a la red puede invocar `POST /transacciones`. Tampoco hay cifrado en tránsito
(TLS) ni límite de tasa por cliente.
**[PROD]**: autenticación de cliente y de servicio a servicio, TLS, y límite de tasa.

---

## 2.15 Pruebas relacionadas

Los tests viven en [tests/](../tests/) y se ejecutan con `pytest`. Grupos relevantes para
este bloque **[MVP]**:

| Archivo | Qué valida |
|---|---|
| [test_transferencias_acid.py](../tests/test_transferencias_acid.py) | Que la transferencia actualiza ambos saldos, crea exactamente dos movimientos, no mueve dinero cuando el saldo es insuficiente, crea el evento outbox y revierte por completo ante un error intermedio. |
| [test_idempotencia.py](../tests/test_idempotencia.py) | Clave nueva, misma clave con mismo hash, misma clave con hash distinto, y el comportamiento de `obtener_o_crear_transaccion` (creación, reutilización y detección de conflicto). |
| [test_concurrencia.py](../tests/test_concurrencia.py) | Doble gasto concurrente, transferencias cruzadas sin deadlock, muchos débitos sobre el mismo origen, cambio concurrente en la cuenta destino, reversión del crédito cuando el destino tiene el UUID menor, y que un reintento no duplica la transacción. |
| [test_concurrencia_fase1.py](../tests/test_concurrencia_fase1.py) | Orden de commit y métricas, rollback ante excepción, reintento completo con la misma clave, reintentos agotados, devolución de conexiones al pool, pool agotado, timeout en el `UPDATE`, 503 del endpoint y clasificación del rechazo en el fallback. |
| [test_ciclo_vida_conexion.py](../tests/test_ciclo_vida_conexion.py) | Que las conexiones rotas se descartan, que las sanas se reutilizan tras conflictos transitorios, que un `rollback` fallido no oculta la excepción original y que el pool conserva capacidad tras descartar una conexión. |
| [test_database.py](../tests/test_database.py) | Directamente contra PostgreSQL: existencia de tablas, `CHECK` de saldo y monto, unicidad de idempotencia, transiciones de estado permitidas y prohibidas, exigencia de `completado_en` e inmutabilidad de los campos financieros. |
| [test_api_transacciones.py](../tests/test_api_transacciones.py) | Contrato HTTP: health, rechazo de monto negativo y de misma cuenta, cuenta inexistente, flujo extremo a extremo e idempotencia sobre HTTP. |
| [test_errores_http.py](../tests/test_errores_http.py) | Que cada causa produce el código correcto (404, 409, 422, 500, 503) y que se registra la métrica de error con el motivo correspondiente, incluidos los tres motivos de pool agotado. |
| [test_cuentas_api.py](../tests/test_cuentas_api.py) | Que el listado solo expone campos seguros, que la búsqueda está parametrizada y trata los comodines como texto, y la resolución de cuentas existentes, inexistentes e inactivas. |
| [test_outbox.py](../tests/test_outbox.py), [test_outbox_concurrencia.py](../tests/test_outbox_concurrencia.py) | Reclamo de eventos pendientes, marcado como publicado, reintentos, marcado como fallido al tercer fallo y que dos workers no reclaman el mismo evento. |

Parte de estas pruebas requieren una instancia real de PostgreSQL; otras usan dobles de
prueba. No se reproduce aquí ningún recuento de pruebas ejecutadas: el estado vigente es
el que arroje `pytest` en el entorno de quien lo ejecute.

---

## 2.16 Efecto de las decisiones del backend en el rendimiento

Sin cifras: las mediciones pertenecen a
[08_pruebas_rendimiento.md](08_pruebas_rendimiento.md). Lo que sí se puede afirmar por
diseño **[MVP]**:

- **Menos viajes en el camino feliz.** Sin consulta previa de idempotencia y sin `SELECT`
  de bloqueo, una transferencia exitosa ejecuta el `SELECT` de idempotencia, el `INSERT`
  de la transacción, dos `UPDATE`, dos `INSERT` de movimiento, un `UPDATE` de estado y un
  `INSERT` de outbox, todo en un solo préstamo de conexión.
- **Bloqueos sostenidos menos tiempo.** Los bloqueos de fila se toman en el `UPDATE` y se
  liberan en el `COMMIT`; no se ejecuta SQL después del commit, así que la conexión vuelve
  al pool sin abrir otra transacción.
- **Degradación predecible.** Ante saturación el sistema rechaza rápido (503) en lugar de
  encolar; el límite real de concurrencia por worker es el tamaño del pool (15), no el del
  threadpool (40).
- **Coste del rechazo.** Una transferencia rechazada paga una consulta adicional con
  bloqueo explícito respecto de una completada.
- **Outbox fuera del camino crítico.** La latencia del `POST` no incluye RabbitMQ ni IA;
  el coste dentro de la transacción es un único `INSERT`.
- **Instrumentación con coste.** Cada operación SQL, préstamo de conexión y fase del
  threadpool se mide; es deliberado para poder diagnosticar, pero no es gratuito.

**[LIM]** conocidos con impacto en rendimiento: el worker de outbox sondea cada segundo y
corre como instancia única; las métricas no están agregadas entre los 5 workers de
Uvicorn (ver
[OBSERVABILIDAD_RENDIMIENTO.md](../servicio_transacciones/OBSERVABILIDAD_RENDIMIENTO.md)).
