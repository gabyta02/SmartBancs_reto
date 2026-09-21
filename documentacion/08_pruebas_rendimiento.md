# 8. Pruebas y rendimiento

Este documento describe la estrategia de pruebas de SmartBancs, cómo reproducirla y qué
puede y no puede afirmarse sobre su rendimiento.

Etiquetas: **[MVP]** implementado y verificable en el código · **[MEDIDO]** resultado
respaldado por evidencia versionada en el repositorio · **[OBJETIVO]** requisito del reto,
no una capacidad demostrada · **[LIM]** limitación actual.

> **Advertencia sobre cifras.** Este documento **no contiene resultados de ejecuciones de
> k6**, porque el repositorio no conserva ningún artefacto versionado con esos resultados
> (ver 8.16). Las tablas de resultados están vacías a propósito: rellenarlas sin evidencia
> sería inventar.

---

## 8.1 Estrategia de pruebas

Cuatro niveles reales en el repositorio **[MVP]**:

| Nivel | Qué valida | Dependencias | Dónde |
|---|---|---|---|
| **Unitarias** | Funciones puras y lógica aislada | Ninguna | `tests/test_etl.py`, `tests/test_servicio_ia.py`, parte de `test_idempotencia.py` |
| **Integración con dobles** | Comportamiento del servicio con dependencias simuladas | Ninguna | `test_errores_http.py`, `test_ciclo_vida_conexion.py`, `test_concurrencia_fase1.py`, `test_demo_web.py`, `test_procesador_eventos.py`, `test_publicador_rabbitmq.py`, `test_trazabilidad_distribuida.py` |
| **Integración con PostgreSQL real** | Flujo transaccional, esquema, idempotencia, outbox | **PostgreSQL en marcha** | 10 archivos (8.5) |
| **Carga** | Comportamiento bajo tráfico sostenido | Entorno completo + k6 | `tests/carga/carga_transacciones.js` |

Dentro de la integración con PostgreSQL hay además **pruebas funcionales de
concurrencia**, que fuerzan simultaneidad real con hilos y barreras (8.8). Son pruebas de
**corrección bajo concurrencia**, no de rendimiento: verifican que el resultado sea
correcto, no cuánto tarda.

**Sobre "E2E"**: el repositorio **no contiene una prueba extremo a extremo** que recorra
el pipeline completo con RabbitMQ y el consumidor de IA reales **[LIM]**. Lo más cercano
es `test_api_transacciones.py::test_transferencia_e2e_completa`, que recorre HTTP →
servicio → PostgreSQL con base de datos real, pero **se detiene en el evento outbox**: no
levanta broker ni consumidor. La propagación de contexto entre worker y consumidor se
valida aparte, con dobles de prueba, en `test_trazabilidad_distribuida.py`. Llamar E2E a
ese conjunto sería inexacto.

---

## 8.2 Ejecución de pytest

Configuración real: [pytest.ini](../pytest.ini) contiene únicamente `pythonpath = .`, lo
que permite importar los paquetes del proyecto desde la raíz del repositorio.

```bash
pytest -q
```

[tests/conftest.py](../tests/conftest.py) hace una sola cosa: fija
`OTEL_TRACES_EXPORTER=none` para que las pruebas no intenten enviar trazas a Jaeger
**[MVP]**.

**Prerrequisitos reales:**

1. Dependencias instaladas: `pip install -r requirements.txt` (agrega los requirements de
   todos los servicios más `pytest` y `python-dotenv`).
2. Un archivo `.env` en la raíz, con las variables de
   [.env.example](../.env.example) completadas. Varias pruebas llaman a `load_dotenv()`.
3. **PostgreSQL accesible** para las pruebas de integración.

**[LIM] importante**: no hay `skipif` en ninguna prueba. Sin PostgreSQL disponible, las
pruebas de integración **fallan**, no se omiten. Una ejecución "en verde" exige el entorno
levantado.

---

## 8.3 Ejecución desde el host frente a dentro de Docker

Las pruebas leen el host de base de datos así **[MVP]**:

```python
DB_HOST = os.getenv("POSTGRES_HOST", "127.0.0.1")
```

| Escenario | Valor correcto | Motivo |
|---|---|---|
| `pytest` desde el host (Windows, macOS, Linux) | `127.0.0.1` (valor por defecto) | El contenedor publica `${POSTGRES_PORT}:5432` |
| Ejecución dentro de la red de Docker | `postgres` | Es el nombre del servicio en `docker-compose.yml` |

El flujo normal es **levantar el entorno con Docker Compose y ejecutar `pytest` desde el
host**, sin exportar `POSTGRES_HOST`: el valor por defecto ya es el correcto.

Detalle a tener en cuenta: **`POSTGRES_HOST` no está en `.env.example`** **[LIM]**. La
variable que sí aparece es `DB_HOST` (usada por los servicios, con valor `127.0.0.1`).
Funciona porque el valor por defecto del código coincide, pero es una inconsistencia de
nombres que conviene conocer.

Las credenciales salen de `.env` (`POSTGRES_USER`, `POSTGRES_PASSWORD`, `DB_APP_USER`,
`DB_APP_PASSWORD`) y **no deben escribirse en la documentación ni en los comandos**.

---

## 8.4 Cobertura por áreas

Resumen por área, sin enumerar cada prueba. El detalle de las pruebas del núcleo
transaccional está en [02_backend_base_datos.md](02_backend_base_datos.md#215-pruebas-relacionadas).

**Unitarias [MVP]**

- **ETL**: cada función de normalización por separado (texto, divisa, tipo, monto, fecha) y
  el proceso completo con casos deliberados de datos sucios
  ([test_etl.py](../tests/test_etl.py)).
- **Modelo de IA**: los tres niveles de score, el efecto de la divisa, la normalización del
  tipo y el rechazo de monto negativo, cero, inválido y UUID malformado
  ([test_servicio_ia.py](../tests/test_servicio_ia.py)).
- **Idempotencia**: `validar_idempotencia` y el hash de solicitud.
- **Publicador y procesador de eventos**: construcción del mensaje y despacho por tipo de
  evento.

**Integración con dobles [MVP]**

- **Errores HTTP**: que cada causa produzca 404, 409, 422, 500 o 503 y registre su métrica
  con el motivo correcto, incluidos los tres motivos de pool agotado
  ([test_errores_http.py](../tests/test_errores_http.py)).
- **Ciclo de vida de conexiones**: descarte de conexiones rotas, reutilización tras
  conflictos transitorios, y que un `rollback` fallido no oculte la excepción original.
- **Trazabilidad distribuida**: relación padre/hijo exacta entre los spans del servicio,
  el worker y el consumidor, y que un mensaje sin contexto siga procesándose.
- **Interfaz de demostración**: proxy de transferencias, no exposición de UUID, y el
  endpoint de ETL contra el ETL real.

**Integración con PostgreSQL real [MVP]**

- **ACID**: saldos, dos movimientos por transferencia, no movimiento de dinero ante saldo
  insuficiente, creación del evento outbox y reversión completa ante error intermedio.
- **Esquema**: existencia de tablas, `CHECK` de saldo y monto, unicidad de idempotencia,
  transiciones de estado permitidas y prohibidas, e inmutabilidad de los campos
  financieros ([test_database.py](../tests/test_database.py)).
- **API con base real**: flujo completo hasta el outbox e idempotencia sobre HTTP.
- **Outbox**: reclamo, publicación, reintentos y marcado como fallido al tercer intento.

---

## 8.5 Pruebas que requieren PostgreSQL

Diez archivos abren conexiones reales **[MVP]**:

`test_database.py`, `test_api_transacciones.py`, `test_transferencias_acid.py`,
`test_idempotencia.py`, `test_concurrencia.py`, `test_cuentas_api.py`, `test_outbox.py`,
`test_outbox_concurrencia.py`, `test_outbox_worker.py`, `test_ciclo_vida_conexion.py`.

`test_database.py` se conecta con **dos usuarios distintos**: el administrador
(`POSTGRES_USER`), para comprobar estructura, constraints y triggers, y el usuario de
aplicación (`DB_APP_USER`), para verificar que sus permisos son los esperados. Esa
separación es parte de lo que se está probando.

Ninguna de estas pruebas requiere RabbitMQ: el flujo asíncrono se valida a nivel de tabla
`eventos_outbox` y con dobles para la publicación **[LIM]**.

---

## 8.6 Número de pruebas

**El repositorio no conserva un artefacto versionado con el resultado de la última
ejecución** **[LIM]**. El número exacto depende del estado actual del repositorio y se
verifica ejecutando:

```bash
pytest -q
```

Dos referencias que existen, y por qué **no** deben citarse como resultado:

- El [README.md](../README.md) declara `188 passed`. Es una afirmación del documento, no
  una evidencia reproducible: no hay salida guardada ni fecha asociada.
- El directorio `.pytest_cache/` contiene el rastro de una ejecución local, pero **está en
  `.gitignore`** (no se versiona) y **está desactualizado**: registra identificadores de
  pruebas que ya no existen con ese nombre en el código actual. No sirve como evidencia.

Lo correcto es ejecutar la suite y adjuntar la salida como evidencia fechada.

---

## 8.7 Pruebas de idempotencia

Escenarios realmente cubiertos **[MVP]**
([test_idempotencia.py](../tests/test_idempotencia.py),
[test_api_transacciones.py](../tests/test_api_transacciones.py)):

| Escenario | Resultado esperado y verificado |
|---|---|
| Clave nueva | Se procesa la transferencia |
| Misma clave + mismo payload | Se devuelve la transacción almacenada, sin mover saldos |
| Misma clave + payload distinto | `ConflictoIdempotencia` → HTTP 409 |
| Reutilización de una transacción existente | `obtener_o_crear_transaccion` devuelve `creada = False` |
| Detección de conflicto en la capa de servicio | El hash almacenado se compara con el recibido |
| Idempotencia sobre HTTP | `test_transferencia_http_es_idempotente` repite la petición completa |
| Reintento interno tras un fallo | `test_retry_tras_primer_update_no_duplica_transaccion` comprueba que un reintento no crea una segunda transacción |

---

## 8.8 Pruebas de concurrencia

Son pruebas **funcionales**, con PostgreSQL real, hilos y `threading.Barrier` para forzar
simultaneidad efectiva **[MVP]** ([test_concurrencia.py](../tests/test_concurrencia.py)):

| Prueba | Escenario | Qué verifica |
|---|---|---|
| `test_no_permite_doble_gasto_concurrente` | Cuenta origen con saldo 100; **dos** transferencias simultáneas de 80 hacia destinos distintos | Solo una puede completarse; el saldo nunca queda negativo |
| `test_transferencias_cruzadas_no_generan_deadlock` | A→B y B→A simultáneas sobre las mismas dos cuentas | El orden determinista por UUID evita el deadlock |
| `test_muchos_debitos_mismo_origen_solo_parte_completa` | Cuenta con saldo 100; **8** transferencias simultáneas de 30 | Solo parte se completa; el resto se rechaza por saldo insuficiente; la suma cuadra |
| `test_cambio_concurrente_destino_se_rechaza` | La cuenta destino cambia de estado o divisa durante la operación | Se clasifica el rechazo correctamente |
| `test_destino_uuid_menor_debito_fallido_revierte_credito` | El destino tiene UUID menor y se acredita primero, pero el débito falla | El savepoint revierte el crédito ya aplicado |
| `test_dos_workers_no_reclaman_mismo_evento` | Dos workers reclaman del outbox a la vez | `FOR UPDATE SKIP LOCKED` impide el doble reclamo |

La distinción importa para no exagerar: estas pruebas demuestran **corrección bajo
concurrencia** con un puñado de hilos. No dicen nada sobre capacidad; eso corresponde a la
prueba de carga.

---

## 8.9 Configuración real de k6

Script: [tests/carga/carga_transacciones.js](../tests/carga/carga_transacciones.js)
**[MVP]**.

**Escenario:**

| Parámetro | Valor |
|---|---|
| `executor` | `constant-arrival-rate` |
| `rate` | `RATE` (por defecto **100**) |
| `timeUnit` | `1s` |
| `duration` | `DURATION` (por defecto **30s**) |
| `preAllocatedVUs` | `PRE_VUS` (por defecto **100**) |
| `maxVUs` | `MAX_VUS` (por defecto **2000**) |

No hay `stages` ni `gracefulStop` explícito: se usa el valor por defecto de k6.

**Variables de entorno reconocidas:**

| Variable | Por defecto | Uso |
|---|---|---|
| `BASE_URL` | `http://localhost:8000` | Destino de las peticiones |
| `RATE` | `100` | Iteraciones solicitadas por segundo |
| `DURATION` | `30s` | Duración del escenario |
| `DATASET` | `HOT` | `HOT` o `LOAD`; cualquier otro valor lanza error |
| `PRE_VUS` | `100` | VUs preasignados |
| `MAX_VUS` | `2000` | Techo de VUs |
| `DEBUG` | `false` | Imprime el detalle de las iteraciones no completadas |

**Petición**: `POST {BASE_URL}/transacciones` con `monto: 0.01`, `divisa: "USD"`, timeout
de cliente de **10 s** y clave de idempotencia única por iteración
(`k6-<dataset>-<timestamp>-<VU>-<iteración>`). El monto mínimo es deliberado: permite
muchas transferencias sin agotar saldos.

**Selección de cuentas**: determinista a partir del número de iteración
(`indiceOrigen = i % n`, `indiceDestino = (i*7+3) % m`), con corrección si origen y destino
coinciden. Al ser determinista, dos ejecuciones con los mismos parámetros generan el mismo
patrón de acceso, lo que hace comparables los resultados.

**Métricas personalizadas** que define el script: `transacciones_completadas`,
`transacciones_rechazadas`, `errores_tecnicos` (contadores) y
`tasa_transacciones_completadas` (rate).

**Checks**: `HTTP 201`, `transferencia completada o rechazada`, y
`respuesta menor a 2 segundos`.

---

## 8.10 `constant-arrival-rate`: qué significa

El ejecutor `constant-arrival-rate` intenta **iniciar** `RATE` iteraciones por segundo,
con independencia de cuánto tarde cada una. Es un modelo de **llegadas**, no de usuarios
concurrentes.

Consecuencias que hay que tener presentes al leer cualquier resultado:

- **`RATE` no es el número de usuarios simultáneos.** Los VUs se asignan según haga falta,
  entre `preAllocatedVUs` y `maxVUs`.
- **`RATE` no es TPS.** Es la tasa *solicitada*; el rendimiento conseguido puede ser
  menor.
- Si el sistema responde más despacio, k6 necesita más VUs para sostener la tasa. Si
  alcanza `maxVUs` y aún no le bastan, **no puede iniciar** todas las iteraciones y las
  contabiliza como `dropped_iterations`.

Este modelo es el adecuado para esta prueba: mide cómo responde el sistema ante una tasa de
llegada dada, que es como se comporta un pico real de tráfico, en lugar de autolimitarse
según la lentitud del propio sistema (que es lo que ocurre con un ejecutor basado en VUs
fijos).

---

## 8.11 Datasets

Dos datasets, con propósitos distintos **[MVP]**:

| Dataset | Qué contiene | Para qué sirve |
|---|---|---|
| **HOT** (por defecto) | 23 UUID incrustados en el script (21 como origen, 23 como destino) | Concentra el tráfico en **pocas cuentas**: maximiza la contención de bloqueos sobre las mismas filas. Es el escenario que más se parece a un pico de quincena con pocas cuentas pagadoras |
| **LOAD** | 5.000 UUID en [tests/carga/cuentas_load.json](../tests/carga/cuentas_load.json) | Distribuye el tráfico sobre **muchas cuentas**: minimiza la contención y mide capacidad con bloqueos dispersos |

La diferencia es exactamente esa —concentración frente a dispersión— y por eso los dos son
necesarios: **HOT** mide el comportamiento ante contención, **LOAD** mide capacidad en
condiciones favorables. Comparar ambos con el mismo `RATE` es la forma de cuantificar
cuánto cuesta la contención.

**[LIM] de los datasets**: ambos contienen **UUID fijos**, que corresponden a las cuentas
de una base de datos concreta. Al recrear el volumen de PostgreSQL, `gen_random_uuid()`
genera identificadores nuevos y **los UUID de HOT dejan de existir**; el script devolvería
404. `cuentas_load.json` tiene el mismo problema: sus 5.000 UUID deben corresponder a las
cuentas `LOAD-%` realmente creadas. Antes de ejecutar una prueba hay que verificarlo
(8.13).

---

## 8.12 Seed de cuentas de carga

Archivo real: **[base_datos/cuentas_carga.sql](../base_datos/cuentas_carga.sql)**.

> **Discrepancia con la documentación previa [LIM]:** el README menciona
> `base_datos/seeds/cuentas_carga.sql` y un fragmento de trabajo mencionaba
> `base_datos/seeds/carga.sql`. **No existe el directorio `base_datos/seeds/`.** La ruta
> correcta es `base_datos/cuentas_carga.sql`.

Contenido verificado:

```sql
INSERT INTO cuentas (numero_cuenta, nombre_titular, saldo, divisa, estado)
SELECT 'LOAD-' || LPAD(i::text, 6, '0'), 'Usuario Carga ' || i, 100000.00, 'USD', 'ACTIVA'
FROM generate_series(1, 5000) AS g(i)
ON CONFLICT (numero_cuenta) DO NOTHING;
```

| Aspecto | Valor |
|---|---|
| Cantidad | **5.000** cuentas |
| Formato | `LOAD-000001` … `LOAD-005000` |
| Saldo inicial | 100000.00 USD |
| Estado | `ACTIVA` |
| Idempotente | Sí: `ON CONFLICT (numero_cuenta) DO NOTHING` permite reejecutarlo sin duplicar |

**No está montado** en `docker-entrypoint-initdb.d` (a diferencia de `ddl.sql` y
`dml.sql`), por lo que se ejecuta manualmente **[MVP]**.

---

## 8.13 Preparación reproducible de la prueba

1. **Levantar la infraestructura**

   ```bash
   docker compose up -d --build
   docker compose ps
   ```

2. **Cargar el seed de cuentas** (PowerShell; ajustar usuario y base a los de `.env`):

   ```powershell
   Get-Content .\base_datos\cuentas_carga.sql -Raw |
     docker exec -i smartbanks_database psql -U "$env:POSTGRES_USER" -d "$env:POSTGRES_DB"
   ```

   Equivalente en shell POSIX:

   ```bash
   docker exec -i smartbanks_database psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
     < base_datos/cuentas_carga.sql
   ```

3. **Verificar que las cuentas existen**

   ```bash
   docker exec -i smartbanks_database psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
     -c "SELECT count(*) FROM cuentas WHERE numero_cuenta LIKE 'LOAD-%';"
   ```

   Resultado esperado: `5000`.

4. **Verificar que el dataset coincide con la base actual** — paso imprescindible tras
   recrear el volumen:

   ```bash
   docker exec -i smartbanks_database psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
     -c "SELECT count(*) FROM cuentas WHERE numero_cuenta LIKE 'LOAD-%' AND estado = 'ACTIVA';"
   ```

   y comprobar que los UUID de `tests/carga/cuentas_load.json` corresponden a esas cuentas.
   Si no coinciden, el dataset debe regenerarse a partir de la base actual.

5. **Ejecutar k6** (8.14).

6. **Observar las métricas durante la ejecución** (8.19 y 8.20).

7. **Guardar los resultados**: la salida de k6 y capturas de Grafana, fechadas y con los
   parámetros usados (8.16).

---

## 8.14 Ejecución de k6

```bash
k6 run -e DATASET=LOAD -e RATE=400 -e DURATION=60s tests/carga/carga_transacciones.js
```

Escenario de contención, con el dataset concentrado:

```bash
k6 run -e DATASET=HOT -e RATE=200 -e DURATION=60s tests/carga/carga_transacciones.js
```

`RATE` y `DURATION` se ajustan al escenario a evaluar. Si el sistema no sostiene la tasa,
conviene subir `PRE_VUS` para descartar que el límite esté en el generador y no en el
sistema:

```bash
k6 run -e DATASET=LOAD -e RATE=800 -e DURATION=60s -e PRE_VUS=800 \
  tests/carga/carga_transacciones.js
```

Para guardar la salida en un artefacto revisable, k6 admite `--summary-export` y
`--out json=...`. **[LIM]**: el repositorio no incluye hoy ninguna salida guardada.

---

## 8.15 Thresholds e interpretación de métricas

**Thresholds reales del script [MVP]:**

| Threshold | Significado |
|---|---|
| `http_req_failed: rate<0.01` | Menos del 1 % de peticiones fallidas |
| `http_req_duration: p(95)<2000` | El percentil 95 de latencia por debajo de **2000 ms** |

Los siete `http_reqs{status:XXX}: count>=0` **no son criterios de aceptación**: siempre se
cumplen. Están para forzar a k6 a desglosar el conteo por código de estado en el resumen,
que es lo que permite ver cuántos 201, 503 o 409 hubo.

**Cómo leer el resumen de k6:**

| Métrica | Qué significa aquí |
|---|---|
| `http_req_duration` | Latencia de la petición. Interesan `avg`, `med`, `p(90)`, `p(95)` y `max` |
| `http_req_failed` | Proporción de peticiones consideradas fallidas por k6 (status ≥ 400 o error de transporte) |
| `iterations` | Iteraciones **completadas** |
| `dropped_iterations` | Iteraciones que k6 **no pudo iniciar** (8.18) |
| `vus` / `vus_max` | VUs en uso y techo; si se alcanza `vus_max`, el generador está en su límite |
| `checks` | Proporción de comprobaciones superadas |
| `http_reqs{status:...}` | Reparto por código de estado |
| `transacciones_completadas` | Contador propio: respuestas 201 con `estado = "COMPLETADA"` |
| `transacciones_rechazadas` | 201 con `estado = "RECHAZADA"` (rechazo de negocio, **no** error) |
| `errores_tecnicos` | Respuestas distintas de 201 |

**Latencia**: `avg` y `med` describen el caso típico; `p(95)` describe la cola que sufre
una parte significativa de usuarios; `max` señala el peor caso, útil para detectar
episodios puntuales de bloqueo. El promedio puede mantenerse sano mientras el P95 se
degrada: por eso el threshold está sobre el P95 y no sobre la media.

---

## 8.16 Resultados

**El repositorio no conserva actualmente ningún artefacto versionado con resultados de
k6** **[LIM]**: no hay salidas de resumen, ni JSON exportado, ni capturas, ni un documento
con cifras fechadas. La búsqueda en el repositorio solo devuelve `cuentas_load.json`, que
es dato de entrada, no resultado.

En consecuencia, **este documento no afirma ninguna cifra de rendimiento**.

Plantilla para registrar resultados reales cuando se ejecuten. Rellenar **solo** con
valores respaldados por una salida guardada:

| Escenario | Dataset | RATE | Duración | Iteraciones completadas | TPS efectivo | P95 (ms) | `http_req_failed` | Dropped | Thresholds | Resultado |
|---|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | | |
| | | | | | | | | | | |

Cada fila debería acompañarse de: fecha, hardware y sistema operativo, versión de k6,
versión del repositorio (commit) y captura de Grafana de la ventana correspondiente. Sin
ese contexto, una cifra de TPS no es reproducible ni comparable.

---

## 8.17 Qué se considera TPS

Definición para este sistema: **transferencias financieras confirmadas por segundo**, es
decir respuestas `201` con `estado = "COMPLETADA"`, medidas sobre la duración del
escenario.

No es lo mismo que:

- **`RATE`**, que es la tasa *solicitada* por k6;
- **`http_reqs/s`**, que incluye rechazos de negocio (409, 422), errores (500, 503) y
  transferencias rechazadas por saldo o estado;
- **`iterations/s`**, que cuenta iteraciones completadas con independencia de su
  resultado.

El script ya separa esos casos con `transacciones_completadas`,
`transacciones_rechazadas` y `errores_tecnicos`, precisamente para que el TPS pueda
calcularse sin ambigüedad.

**Matiz necesario**: una transferencia `RECHAZADA` por saldo insuficiente es una respuesta
**correcta** del sistema —trabajo hecho, no error— pero **no** es una transferencia
completada. Contarla como TPS inflaría la cifra; contarla como error también sería
incorrecto. Por eso se mide aparte.

---

## 8.18 Interpretación de errores

| Resultado | Naturaleza | Lectura |
|---|---|---|
| `201` con `COMPLETADA` | Éxito | Transferencia confirmada |
| `201` con `RECHAZADA` | **Funcional** | Regla de negocio aplicada (saldo, estado, divisa). Con `monto: 0.01` y el seed a 100000.00 no debería aparecer, salvo que el dataset no corresponda a la base |
| `404` | Funcional | Cuenta inexistente: **señal de que el dataset no coincide con la base de datos** |
| `409` | Funcional | Conflicto de idempotencia. No debería ocurrir: el script genera claves únicas |
| `422` | Funcional | Validación del payload |
| `500` | **Infraestructura** | Error de PostgreSQL no reintentable o excepción no controlada. Siempre requiere investigación |
| `503` | **Saturación** | Pool agotado o deadlock/timeout tras agotar reintentos. Es la degradación **esperada y controlada** del sistema |
| `status: 0` | **Infraestructura** | Sin respuesta: timeout del cliente (10 s), conexión rechazada o red |
| `dropped_iterations` | **Generador** | k6 no logró iniciar las iteraciones solicitadas |

La distinción clave para evaluar una prueba: un 503 indica que **el sistema se protegió**,
devolviendo una respuesta rápida y reintentable en lugar de degradarse sin control. Un 500
o un `status: 0` indican que **algo falló de verdad**. Ambos cuentan como fallo para
`http_req_failed`, pero significan cosas opuestas.

**`dropped_iterations` merece cuidado**: significa que k6 no pudo iniciar iteraciones al
ritmo pedido, por haber agotado `maxVUs` o por límite del propio generador. **No se debe
atribuir automáticamente al backend.** Antes de concluir nada hay que repetir la prueba con
`PRE_VUS` y `MAX_VUS` más altos y comprobar si desaparecen. Si desaparecen, el límite
estaba en el generador; si persisten con VUs de sobra, el sistema no sostiene la tasa.

---

## 8.19 El objetivo de 10.000 TPS

El reto plantea un escenario de alta concurrencia, "p. ej., 10.000 transacciones por
segundo".

**Estado real:**

| | |
|---|---|
| **[OBJETIVO]** del escenario del reto | 10.000 TPS |
| **[MEDIDO]** en este repositorio | **Ninguna cifra**: no hay artefacto de resultados versionado (8.16) |

Por tanto **no se afirma que SmartBancs soporte 10.000 TPS**. No existe en el repositorio
una prueba reproducible que lo demuestre, y sin esa evidencia la afirmación sería falsa.

Lo que sí puede afirmarse con honestidad:

- Existe una **prueba de carga reproducible y parametrizable** que permite medir la
  capacidad real en cualquier entorno.
- Existen **dos datasets** que permiten separar el efecto de la contención del de la
  capacidad bruta.
- Existe **observabilidad suficiente** para identificar dónde está el límite cuando se
  alcance (8.20).
- La arquitectura **no impide** escalar: el procesamiento asíncrono está desacoplado y el
  worker admite varias instancias. Pero "no impide escalar" **no es** "escala a 10.000
  TPS": son afirmaciones distintas y solo la primera está respaldada.

Tampoco se afirma que el sistema alcanzaría esa cifra con mejor hardware. Cualquier
resultado obtenido corresponde **al entorno en que se midió** y no constituye una
validación de 10.000 TPS.

---

## 8.20 Los dos requisitos son dimensiones distintas

El reto plantea dos exigencias que conviene no mezclar:

| Requisito | Dimensión | Cómo se mide aquí |
|---|---|---|
| "Menos de 2 segundos" | **Latencia** | Threshold `p(95)<2000` y el check por petición |
| "10.000 transacciones por segundo" | **Throughput** | TPS efectivo (8.17) |

Un sistema puede cumplir la latencia con poco throughput (rápido, pero para pocos) o tener
alto throughput con mala latencia (muchos atendidos, todos despacio). **Hay que evaluar
ambas a la vez, y por eso el resultado de una prueba de carga se lee como par
(TPS efectivo, P95)**, nunca como un número suelto.

**Interpretación correcta del umbral de 2 segundos**: `p(95) < 2000 ms` significa que **al
menos el 95 % de las peticiones medidas quedaron por debajo de 2 segundos**. No significa
que todas lo hicieran. El script mide además el cumplimiento individual con el check
`respuesta menor a 2 segundos`, cuya proporción aparece en la sección `checks` del resumen:
ese dato es el que permite hablar del porcentaje real de peticiones dentro del umbral.

---

## 8.21 Qué observar durante la prueba

Una prueba de carga sin observar el sistema solo produce un número. Simultáneamente a k6,
en Grafana y Prometheus **[MVP]** (detalle en [05_observabilidad.md](05_observabilidad.md)):

| Señal | Métrica | Qué indicaría |
|---|---|---|
| Throughput | `sum(rate(smartbancs_transacciones_total[1m]))` | Si se aplana mientras `RATE` sube, se alcanzó el techo |
| Latencia | P95 de `smartbancs_transferencia_duracion_seconds` | Debe contrastarse con el `http_req_duration` de k6 |
| Errores por tipo | `smartbancs_transacciones_errores_total{tipo}` | Separa deadlock, timeout y error de BD |
| Pool | `smartbancs_db_pool_prestadas`, `..._agotado_total`, `..._rechazos_total{motivo}` | Si el pool es el límite, aquí se ve primero |
| Threadpool | `smartbancs_threadpool_tasks_waiting` | Saturación de la capa síncrona |
| SQL por operación | `smartbancs_db_sql_duracion_seconds{operacion}` | Qué sentencia se degrada |
| Bloqueos | `smartbancs_db_lock_wait_seconds` | Efecto directo del dataset HOT |
| Deadlocks | `smartbancs_db_deadlocks_total` | Contención severa |
| Outbox | `smartbancs_outbox_eventos_total{resultado}` | Si el worker sigue el ritmo de generación de eventos |
| IA | `smartbancs_ia_mensajes_total{resultado}` | Backlog de análisis |

**En PostgreSQL**, durante la prueba: número de conexiones y su reparto por
`application_name`, consultas activas y su duración (`pg_stat_activity`), esperas por
bloqueo (`wait_event_type = 'Lock'` y `pg_blocking_pids`), sentencias más costosas
(`pg_stat_statements`) y aparición de deadlocks en el log del servidor. Las consultas están
en [06_incidente_critico.md](06_incidente_critico.md#68-diagnóstico-directo-en-postgresql).

**[LIM]**: no hay métricas de CPU ni memoria por contenedor, así que descartar saturación
de recursos exige `docker stats` a mano.

---

## 8.22 Metodología de carga incremental

Metodología propuesta, no resultados. Consiste en aumentar la tasa por pasos manteniendo
constante todo lo demás, hasta encontrar el punto en que el sistema deja de sostener la
tasa:

1. Empezar con un `RATE` bajo y una duración suficiente para que el sistema se estabilice
   (los primeros segundos incluyen calentamiento de conexiones).
2. Subir la tasa por pasos: por ejemplo 100 → 200 → 400 → 800, con la **misma duración**
   en cada paso.
3. En cada paso registrar: TPS efectivo, P95, reparto por código de estado,
   `dropped_iterations` y las métricas de 8.21.
4. **El punto de saturación** es aquel en que, al subir `RATE`, el TPS efectivo deja de
   crecer mientras el P95 o los errores suben. Ahí está la capacidad, y las métricas del
   sistema dicen **cuál** es el recurso que la limita.
5. Repetir con **ambos datasets** para cuantificar el coste de la contención.
6. Verificar que `dropped_iterations` sea cero antes de atribuir cualquier límite al
   sistema (8.18).

**Otros tipos de prueba, no realizados [LIM]:**

- **Estrés**: llevar el sistema deliberadamente más allá de su capacidad para observar
  cómo se degrada y si se recupera al cesar la carga. Distinto de la prueba de carga, que
  valida el comportamiento bajo la carga esperada. **No hay evidencia de que se haya
  ejecutado.**
- **Soak**: carga sostenida durante un periodo largo (horas) para detectar fugas de
  memoria, crecimiento descontrolado de `eventos_outbox`, degradación progresiva o
  agotamiento lento del pool. **[PROD]**, no ejecutada.

---

## 8.23 Cuándo una prueba es válida

Una ejecución no es exitosa solo porque terminó. Debe revisarse:

1. **`dropped_iterations` = 0**, o justificado. Si no, la tasa real no fue la configurada.
2. **TPS efectivo**, no `RATE`.
3. **Thresholds**: `http_req_failed` y `p(95)`, con su veredicto de k6.
4. **Reparto por código de estado**: un 503 tiene lectura distinta de un 500.
5. **Checks**: proporción de peticiones bajo 2 segundos.
6. **Coherencia con las métricas del sistema**: si k6 y Prometheus no cuentan lo mismo,
   hay que entender por qué antes de publicar cualquier cifra (recordar que `/metrics`
   refleja un solo worker de Uvicorn por scrape).
7. **Consistencia financiera posterior** (8.24).

---

## 8.24 Verificación de consistencia tras la prueba

Una prueba de carga sobre un sistema financiero debe terminar comprobando que **el dinero
cuadra**, no solo que la latencia fue aceptable. Las consultas de solo lectura están en
[06_incidente_critico.md](06_incidente_critico.md#618-validación-de-integridad); aplicadas
aquí:

- transferencias `COMPLETADA` **sin sus dos movimientos**;
- transferencias `COMPLETADA` **sin evento outbox**;
- transacciones que quedaron en `PENDIENTE`;
- movimientos cuyo importe no coincide con el de su transacción;
- **claves de idempotencia duplicadas**: el `UNIQUE` lo impide, pero conviene confirmar que
  el número de transacciones creadas coincide con el de iteraciones completadas;
- saldos negativos (el `CHECK` los impide; es una confirmación);
- estado del backlog: eventos `PENDIENTE`, `PROCESANDO` y `FALLIDO` en `eventos_outbox`, y
  transferencias completadas sin análisis de IA.

Tras una prueba con `RATE` alto, es esperable un backlog transitorio de eventos y
análisis: el worker sondea cada segundo en lotes de 20, así que su capacidad de drenaje es
acotada por diseño. Lo relevante no es que exista backlog, sino que **se drene** al cesar
la carga.

---

## 8.25 Limitaciones del entorno de prueba

Verificables en la configuración del repositorio:

1. **Ejecución local en un solo host**: todos los servicios corren en la misma máquina con
   Docker Compose **[LIM]**.
2. **El generador de carga comparte máquina** con el sistema bajo prueba, salvo que se
   ejecute k6 desde otro equipo apuntando con `BASE_URL`. Compiten por CPU **[LIM]**.
3. **Una sola instancia de PostgreSQL**, sin réplica ni balanceo **[LIM]**.
4. **Una sola instancia de cada servicio**, sin balanceador delante, y sin posibilidad de
   usar `docker compose --scale` mientras se declaren `container_name` fijos **[LIM]**.
5. **Sin límites de CPU ni memoria por contenedor**: la competencia entre servicios no está
   acotada ni es observable **[LIM]**.
6. **Métricas no agregadas entre los 5 workers de Uvicorn**, lo que afecta a la lectura de
   contadores durante la prueba **[LIM]**.
7. **Datasets con UUID fijos** que deben corresponder a la base de datos actual (8.11)
   **[LIM]**.

Estas limitaciones no invalidan la prueba: la hacen **acotada a su entorno**. Cualquier
cifra obtenida describe ese entorno, y se debe decir así.

---

## 8.26 Evidencias del reto

El reto solicita material de evidencia. Estado real del repositorio:

| Evidencia | Estado |
|---|---|
| Código fuente y pruebas automatizadas | **Presente** |
| Script de carga reproducible | **Presente** (`tests/carga/carga_transacciones.js`) |
| Datasets de prueba | **Presentes** (`tests/carga/cuentas_load.json`, `base_datos/cuentas_carga.sql`, `etl/datos/*.csv`) |
| Dashboard de monitoreo versionado | **Presente** (`monitoreo/grafana/provisioning/dashboards/`) |
| Salida de `pytest` fechada | **PENDIENTE**: el README declara `188 passed`, sin artefacto que lo respalde |
| Resultados de k6 | **PENDIENTE**: no hay ninguna salida guardada |
| Capturas de Grafana durante carga | **PENDIENTE** |
| Video demostrativo | **PENDIENTE**: no existe en el repositorio |
| Material de presentación | **PENDIENTE**: no existe en el repositorio |

Los cuatro últimos puntos son entregables que deben producirse ejecutando lo que ya existe.
No se han creado evidencias ficticias para rellenarlos.

**Datos de prueba versionados** y su propósito:

| Archivo | Propósito |
|---|---|
| `tests/carga/cuentas_load.json` | 5.000 UUID de cuentas para el dataset LOAD |
| `base_datos/cuentas_carga.sql` | Crea esas 5.000 cuentas `LOAD-%` |
| `base_datos/dml.sql` | 25 cuentas de demostración `SB-000006`…`SB-000030`, con casos límite y estados no activos |
| `etl/datos/transacciones_raw.csv` | 1.999 registros para el ETL |
| `etl/datos/transacciones_prueba.csv` | 1.999 registros con fechas `dd/mm/aaaa` y divisas en minúscula, para ejercitar la normalización |

---

## 8.27 Qué demuestra y qué no demuestra el MVP

**Demuestra [MVP]:**

- Una arquitectura **ejecutable** con un solo comando.
- Un **flujo financiero correcto**: atomicidad, idempotencia y clasificación de rechazos,
  verificado con pruebas automatizadas contra PostgreSQL real.
- **Corrección bajo concurrencia**: doble gasto, transferencias cruzadas y reclamo
  concurrente del outbox, verificados con hilos y barreras.
- **Degradación controlada**: el sistema rechaza con 503 en lugar de colapsar, y eso es
  observable.
- **Observabilidad** suficiente para localizar el cuello de botella por capas.
- Una **prueba de carga reproducible y parametrizable**, con dos datasets que separan
  contención de capacidad.

**No demuestra:**

- **10.000 TPS sostenidos**, ni ninguna otra cifra de capacidad: no hay evidencia
  versionada.
- **Alta disponibilidad**: una sola instancia de PostgreSQL, sin réplica ni failover.
- **Despliegue multinodo, balanceo ni autoescalado.**
- **Rendimiento en infraestructura cloud** o multirregión.
- **Comportamiento en pruebas de estrés o soak**, no ejecutadas.

Esto es **alcance**, no defecto: un MVP debe demostrar que el diseño es correcto y
medible, y eso es lo que está respaldado. Lo que falta para afirmar capacidad no es más
documentación, sino **ejecutar las pruebas y guardar sus resultados**.
