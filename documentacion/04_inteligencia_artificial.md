# 4. Inteligencia Artificial

Este documento describe el componente de análisis de SmartBancs: lo que está implementado en el MVP y lo que sería necesario para llevar un modelo real a producción.

Etiquetas usadas: **[MVP]** implementado y verificable en el código ·
**[PROD]** propuesta para producción, no implementada · **[LIM]** limitación actual.

La **Parte A** describe el código existente. La **Parte B** es diseño propuesto: responde a lo que pide el reto sobre ciclo de vida, alimentación con nuevos datos, data drift y consumo de recursos, y **nada de ella está implementado**.

---

# Parte A — Estado real de la IA en el MVP

## 4.1 Qué es y qué no es

El componente de IA de SmartBancs es **un motor de reglas deterministas**, identificado
como `smartbancs-mock-v1` **[MVP]**.

| Pregunta | Respuesta verificada |
|---|---|
| ¿Existe el servicio? | Sí: [servicio_ia/app/main.py](../servicio_ia/app/main.py), FastAPI `SmartBancs - Servicio IA`, puerto 8002. |
| ¿Usa alguna librería de machine learning? | **No.** Las dependencias de `servicio_ia` son `fastapi`, `uvicorn`, `pydantic`, `pika`, `psycopg2-binary`, `opentelemetry-*` y `prometheus-client`. No hay scikit-learn, ni pandas, ni numpy, ni ningún framework de ML ([requirements.txt](../servicio_ia/requirements.txt)). |
| ¿Hay un modelo entrenado? | **No.** No existe archivo de pesos, ni proceso de entrenamiento, ni dataset de entrenamiento en el repositorio. |
| ¿Qué hace entonces? | Suma tres factores fijos según monto, tipo y divisa; obtiene un score; deriva un nivel; devuelve dos textos de recomendación preescritos. |
| ¿Persiste resultados? | Sí, en la tabla `analisis_ia` de PostgreSQL. |
| ¿Quién lo consulta? | `demo_web`, vía `GET /analisis/{id}/admin` de `servicio_ia`. |

Dicho sin rodeos: **no es un sistema de machine learning**. Es una heurística
determinista con una interfaz estable. Su propósito dentro del MVP es **demostrar la
integración asíncrona**, no la calidad predictiva: el mismo contrato podría respaldarse
mañana con un modelo real sin tocar el resto del sistema. Esa es la parte que el MVP sí
demuestra.

---

## 4.2 El modelo actual, en detalle

Implementación completa en
[servicios/recomendador.py](../servicio_ia/app/servicios/recomendador.py) **[MVP]**:

**Factores** (`calcular_factores`):

| Factor | Regla | Valor |
|---|---|---|
| `monto` | `monto > 1000` | 0.60 |
| | `100 <= monto <= 1000` | 0.30 |
| | `monto < 100` | 0.10 |
| `tipo` | `tipo == "TRANSFERENCIA"` | 0.10 (0.0 en otro caso) |
| `divisa` | `divisa != "USD"` | 0.10 (0.0 en otro caso) |

**Score** (`calcular_score`): suma de los tres factores, acotada con `min(score, 1.0)` y
redondeada a 2 decimales.

**Nivel** (`determinar_nivel`): `score < 0.35` → `BAJO`; `score < 0.70` → `MEDIO`; en otro
caso → `ALTO`.

**Recomendaciones**: dos textos fijos por nivel, uno para el usuario final y otro para el
administrador. No dependen de nada más que del nivel.

Dos consecuencias que conviene conocer y poder explicar **[LIM]**:

1. **El score máximo alcanzable es 0.80** (0.60 + 0.10 + 0.10), por lo que la cota
   `min(score, 1.0)` nunca se activa.
2. **En el flujo asíncrono, el nivel depende únicamente del monto.** El consumidor
   siempre construye la solicitud con `tipo="TRANSFERENCIA"` (valor fijo en el código), y
   las cuentas del MVP operan en USD. Con esos valores: `monto < 100` → 0.20 → `BAJO`;
   `100 ≤ monto ≤ 1000` → 0.40 → `MEDIO`; `monto > 1000` → 0.70 → `ALTO`. El endpoint
   directo `POST /analizar` sí permite ejercitar los otros factores.

El campo `categoria` de la respuesta es el `tipo` normalizado a mayúsculas; **no se
persiste** en `analisis_ia`, que no tiene esa columna **[LIM]**.

---

## 4.3 Flujo asíncrono real

El flujo comprobado en el código, que difiere en un punto del que suele asumirse:

```text
POST /transacciones                       [servicio_transacciones]
   │  transacción ACID: saldos + movimientos + estado
   │  + INSERT en eventos_outbox (misma transacción)
   ▼
COMMIT ───────────────────────────────►  201 al cliente   ← aquí termina el camino crítico
   │
   ▼
eventos_outbox (estado PENDIENTE)         [PostgreSQL]
   │  sondeo cada 1 s · FOR UPDATE SKIP LOCKED · lote de 20
   ▼
worker_outbox  ─── publish ───►  RabbitMQ · cola_ia (durable)
                                     │  prefetch=1 · ack manual
                                     ▼
                                consumidor_ia
                                     │  analizar_transaccion(...)  ← llamada EN PROCESO
                                     ▼
                                analisis_ia            [PostgreSQL]
                                     ▲
   demo_web ── GET /demo/analisis/{id} ──► servicio_ia ─┘
                                          GET /analisis/{id}/admin
```

**Precisión importante [MVP]:** `consumidor_ia` **no llama por HTTP a `servicio_ia`**.
Importa directamente `analizar_transaccion` del módulo `recomendador`
([consumidor.py](../servicio_ia/app/consumidor.py)) y lo ejecuta en su propio proceso.
Ambos contenedores se construyen desde la **misma imagen**: `servicio_ia` arranca la API y
`consumidor_ia` arranca el consumidor con `python -m servicio_ia.app.consumidor`
([docker-compose.yml](../docker-compose.yml)). El puerto 8002 sirve exclusivamente para
**consultar** análisis ya persistidos, no para producirlos.

Lo que el flujo garantiza:

- **La transferencia se confirma antes.** El análisis ocurre después del `COMMIT` y del
  `201`.
- **La IA no participa del commit financiero.** No comparte transacción con los saldos.
- **La IA no bloquea la respuesta.** No hay ninguna llamada a IA dentro del manejador de
  `POST /transacciones`.
- **Una demora en IA no aumenta la latencia del endpoint principal.** Lo que crece es el
  retraso del análisis y, si persiste, la profundidad de la cola.

El acoplamiento restante es indirecto y conviene nombrarlo: `consumidor_ia` escribe en la
**misma instancia de PostgreSQL** que el servicio transaccional, así que compite por
conexiones y recursos del servidor de base de datos **[LIM]**.

---

## 4.4 Consumidor de IA

Comportamiento real de [servicio_ia/app/consumidor.py](../servicio_ia/app/consumidor.py)
**[MVP]**:

**Recepción.** Conexión bloqueante de `pika` con credenciales y host desde entorno
(`RABBITMQ_HOST`, `RABBITMQ_PORT`, `RABBITMQ_USER`, `RABBITMQ_PASSWORD`,
`RABBITMQ_QUEUE_IA`), `queue_declare(durable=True)`, `basic_qos(prefetch_count=1)` y
`basic_consume(auto_ack=False)`. Si la conexión falla, el bucle externo espera 5 segundos
y reintenta indefinidamente.

**Contexto de traza.** `extract(mensaje.get("trace_context", {}))` recupera el contexto
W3C que viajó dentro del mensaje y lo usa como **padre** del span
`consumidor_ia.procesar`, con atributos `messaging.system`, `messaging.destination.name`,
`messaging.operation.name` y `smartbancs.event.type`. Un mensaje sin `trace_context` se
procesa igual, solo que su traza arranca ahí (comportamiento cubierto por
[test_trazabilidad_distribuida.py](../tests/test_trazabilidad_distribuida.py)).

**Datos que utiliza.** `construir_solicitud_ia` rechaza cualquier `tipo_evento` distinto
de `TRANSFERENCIA_COMPLETADA` y construye `SolicitudAnalisis` con:
`id_transaccion` del mensaje, `monto` y `divisa` de `datos`, y `tipo="TRANSFERENCIA"`
**fijo**. El resto de la carga útil (cuentas, estado, `trace_id`) **no se usa**.

**Análisis.** Llama a `analizar_transaccion` dentro del span `ia.analizar_transaccion`,
mide `smartbancs_ia_duracion_seconds` y cuenta `smartbancs_ia_analisis_total{resultado}`.

**Persistencia.** Abre una **conexión nueva por mensaje**, guarda dentro del span
`db.guardar_analisis` y la cierra en `finally`. `guardar_analisis` ejecuta
`INSERT ... ON CONFLICT (transaccion_id) DO UPDATE` y hace `commit()`, o `rollback()` y
relanza si algo falla ([repositorios/analisis.py](../servicio_ia/app/repositorios/analisis.py)).

**Confirmación.** `basic_ack` **solo después** de analizar y persistir correctamente;
entonces incrementa `smartbancs_ia_mensajes_total{resultado="procesado"}`.

**Errores.** Cualquier excepción en el `callback` produce `basic_nack(delivery_tag,
requeue=False)` y `smartbancs_ia_mensajes_total{resultado="rechazado"}`.

Esto último merece precisión, porque es la diferencia entre documentar y adornar
**[LIM]**:

- **No hay reintentos del mensaje.** `requeue=False` descarta.
- **No hay DLQ declarada.** El mensaje descartado se pierde.
- **No hay compensación.** El evento ya está `PUBLICADO` en `eventos_outbox`, así que el
  worker no lo reenviará: esa transferencia se queda **sin análisis, de forma
  permanente**, y la interfaz la mostrará indefinidamente como `PENDIENTE`.
- **No hay alerta específica** para ese caso más allá del contador de mensajes
  rechazados.

---

## 4.5 Contrato del servicio de IA

Definido en [esquemas/analisis.py](../servicio_ia/app/esquemas/analisis.py) **[MVP]**.
No existen campos de perfil financiero: **no hay `id_usuario`, ni `ingresos`, ni `gastos`,
ni `saldo`**. La entrada es exclusivamente la operación.

**Entrada — `SolicitudAnalisis`:**

| Campo | Tipo | Validación |
|---|---|---|
| `id_transaccion` | `UUID` | obligatorio |
| `monto` | `Decimal` | `> 0` |
| `divisa` | `str` | obligatorio, sin restricción de formato |
| `tipo` | `str` | obligatorio, sin catálogo cerrado |

**Salida — `RespuestaAnalisis`:** `id_transaccion`, `categoria`, `nivel`, `score`,
`factores` (`monto`, `tipo`, `divisa`), `recomendacion_usuario`, `recomendacion_admin`,
`modelo`.

**Endpoints** de `servicio_ia`:

| Endpoint | Respuesta |
|---|---|
| `GET /health` | `{"status": "ok", "service": "ia", "modelo": "smartbancs-mock-v1"}` |
| `POST /analizar` | `RespuestaAnalisis` calculada al vuelo. **No persiste**. |
| `GET /analisis/{transaccion_id}/usuario` | `AnalisisUsuarioResponse`: `id_transaccion`, `nivel`, `recomendacion` (la del usuario). `404` si no existe. |
| `GET /analisis/{transaccion_id}/admin` | `AnalisisAdminResponse`: `id_transaccion`, `score`, `nivel`, `factores`, `recomendacion` (la de admin), `modelo`. `404` si no existe. |

La separación usuario/administrador es deliberada: al usuario se le da nivel y consejo; el
score, los factores y el modelo quedan para la vista de administración.

**Persistencia — tabla `analisis_ia`** ([ddl.sql](../base_datos/ddl.sql)):
`id_analisi` (PK), `transaccion_id` (**`UNIQUE`** + FK a `transacciones`), `score
numeric(4,2)`, `nivel varchar(20)`, `factores jsonb`, `recomen_user text`, `recomen_admin
text`, `modelo varchar(100)`, `creado_en`. El `UNIQUE` es lo que hace idempotente al
*upsert* del consumidor.

**Consulta desde `demo_web`:** `GET /demo/analisis/{id_transaccion}` llama a
`/analisis/{id}/admin` y traduce la respuesta: `404` del servicio se convierte en
`{"estado": "PENDIENTE"}` —no es un error, es que el análisis aún no llegó— y `200` en
`{"estado": "DISPONIBLE", nivel, score, modelo, recomendacion}`
([demo_web/app.py](../demo_web/app.py)).

**Pruebas [MVP]:** [test_servicio_ia.py](../tests/test_servicio_ia.py) cubre los tres
niveles, el efecto de la divisa, la normalización del tipo y el rechazo de monto negativo,
cero, inválido y UUID malformado. [test_analisis_ia_endpoints.py](../tests/test_analisis_ia_endpoints.py)
cubre las dos vistas y sus 404.

---

## 4.6 Por qué la IA es asíncrona

El reto exige que *"las recomendaciones de IA no deben bloquear ni retrasar el flujo
transaccional principal"*. La arquitectura lo cumple por construcción **[MVP]**:

- **Desacoplamiento**: el servicio transaccional no conoce al de IA. Escribe un evento en
  una tabla y termina. No hay import, ni llamada HTTP, ni dependencia de arranque.
- **Menor latencia**: el `201` no espera al análisis. Sea cual sea el coste de la
  inferencia, el usuario no lo paga.
- **Aislamiento de fallos**: si el consumidor o la base de datos de análisis fallan, las
  transferencias siguen procesándose. Lo comprobable es que ninguna ruta de error de IA
  alcanza el endpoint financiero.
- **Absorción de picos**: la cola actúa de amortiguador entre una ráfaga de transferencias
  y la capacidad de análisis.
- **Escalabilidad independiente**: el número de consumidores no está atado al de workers
  de Uvicorn del servicio transaccional.

**Trade-offs que hay que asumir:**

- **La recomendación no es inmediata.** La interfaz debe contemplar el estado
  `PENDIENTE`, y lo hace.
- **Consistencia eventual del análisis**: existe una ventana en la que la transferencia
  está confirmada y el análisis todavía no.
- **Hay que seguir el estado**: sin observar la cola y el consumidor, un análisis que
  nunca llega pasa desapercibido.
- **Puede acumularse backlog** si el ritmo de entrada supera al de proceso.
- **Complejidad operativa**: un broker y un proceso más que vigilar.

---

# Parte B — Propuesta para producción

Todo lo que sigue es **diseño propuesto [PROD]**. Nada está implementado en el MVP. Se
describe en términos concretos de SmartBancs para que sea evaluable, no como teoría
general.

## 4.7 Ciclo de vida de un modelo real

| # | Etapa | Qué significaría en SmartBancs |
|---|---|---|
| 1 | **Captura de datos** | Las transferencias ya se registran en `transacciones` y `movimientos`, con importe, divisa, cuentas, estado, motivo de rechazo y marcas de tiempo. Esa es la fuente primaria; no hay que inventarla. |
| 2 | **Ingesta** | Extracción periódica hacia un almacenamiento analítico separado del operativo, para no ejecutar consultas de entrenamiento contra la base que atiende transferencias. |
| 3 | **Validación** | Comprobaciones de esquema y de rango antes de aceptar un lote: tipos, nulos, importes negativos, fechas imposibles, cuentas inexistentes. |
| 4 | **Limpieza** | Normalización de divisa, fecha y monto: exactamente lo que ya hace el ETL actual. |
| 5 | **ETL** | Reutilizar [etl/transformar.py](../etl/transformar.py) como base conceptual, evolucionado según [03_bancs_etl.md](03_bancs_etl.md): staging, incremental y linaje. |
| 6 | **Dataset histórico** | Acumulación de los lotes limpios, particionada por fecha. |
| 7 | **Versionado de datos** | Ver 4.10. |
| 8 | **Feature engineering** | Ver 4.9. |
| 9 | **Entrenamiento** | Ver 4.11. |
| 10 | **Evaluación offline** | Medición sobre un conjunto reservado que el modelo no vio. |
| 11 | **Validación** | Criterios de aceptación explícitos antes de promover. Ver 4.12. |
| 12 | **Registro del modelo** | Ver 4.13. |
| 13 | **Despliegue** | Ver 4.14. |
| 14 | **Inferencia** | Sustituir `analizar_transaccion` manteniendo el contrato `SolicitudAnalisis` → `RespuestaAnalisis`; el campo `modelo` pasa a identificar la versión real. |
| 15 | **Monitoreo** | Ver 4.24. |
| 16 | **Detección de drift** | Ver 4.16 a 4.21. |
| 17 | **Reentrenamiento** | Disparado por decisión, no automáticamente por una alerta. |
| 18 | **Promoción** | Cambio de estado de la versión en el registro, con la aprobación correspondiente. |
| 19 | **Rollback** | Ver 4.15. |

El punto de apoyo real es que el contrato de entrada y salida ya existe y está aislado
detrás de una cola: **cambiar el modelo no obliga a tocar `servicio_transacciones`**.

## 4.8 Cómo se alimentaría el modelo con nuevos datos

```text
transferencias productivas (PostgreSQL)
   → extracción a almacenamiento raw (inmutable, con fecha y origen)
   → validación de esquema y calidad
   → ETL de limpieza y normalización
   → dataset limpio          (válidos)  +  dataset observado (apartados)
   → dataset versionado con ventana temporal y reglas aplicadas
   → entrenamiento / evaluación
```

Principios que deberían respetarse:

- **Los datos productivos no van directamente al entrenamiento.** Entre la producción y el
  modelo hay validación y control de calidad; saltárselo convierte cualquier incidente de
  datos en un incidente de modelo.
- **Los registros observados se excluyen del entrenamiento**, pero se conservan: su
  distribución es una señal de salud del pipeline. El ETL actual ya produce exactamente
  esa separación (`transacciones_limpias.csv` / `transacciones_observadas.csv`), con el
  `motivo_rechazo` de cada registro apartado.
- **Separación entre entrenamiento y producción**: el entrenamiento no debe ejecutarse
  contra la base operativa ni competir por sus recursos.
- **Trazabilidad**: para cada modelo debe poder responderse con qué datos exactos se
  entrenó.

## 4.9 Datos de entrenamiento: disponibles vs propuestos

**Disponibles hoy en el MVP** (existen en el esquema o en el evento):

| Variable | Fuente |
|---|---|
| `monto` | `transacciones.monto` |
| `divisa` | `transacciones.divisa` |
| estado y `razon_fallo` | `transacciones` |
| marca temporal | `creado_en`, `completado_en` |
| cuenta origen y destino | `transacciones` (identificadores) |
| saldo resultante por movimiento | `movimientos.saldo_resultante` |
| tipo de movimiento | `movimientos.tipo` (`DEBITO` / `CREDITO`) |

**Propuestas, derivables de lo anterior [PROD]:** frecuencia de transacciones por cuenta y
ventana, importe medio y desviación respecto al histórico de la cuenta, hora y día de la
semana, antigüedad de la cuenta, número de destinatarios distintos, si el destinatario es
nuevo para esa cuenta, ratio entre el importe y el saldo previo.

**Propuestas que hoy NO existen en el sistema [PROD]:** ingresos, gastos declarados,
perfil o segmento de cliente, historial crediticio, datos de canal o dispositivo. Traerlas
exigiría nuevas fuentes de datos y una revisión de privacidad; no son un simple añadido de
columnas.

El `tipo` del ETL (`PAYMENT`, `TRANSFER`, `CASH_OUT`, …) proviene de los archivos de
ejemplo, no de las transferencias productivas del MVP, que son todas transferencias entre
cuentas.

## 4.10 Versionado de datos

No existe ningún registro de datasets en el MVP **[LIM]**. Propuesta mínima: cada dataset
llevaría un identificador estable y, junto a él, la **ventana temporal** cubierta, la
**fecha de generación**, la **versión de las reglas de limpieza** aplicadas, la **versión
del conjunto de features** y el **hash del contenido**.

Sirve para lo único que importa aquí: **reproducibilidad**. Sin esto, ante un modelo que
se comporta mal no puede responderse "¿con qué datos se entrenó?", y cualquier
investigación se detiene ahí.

## 4.11 Entrenamiento

Conceptualmente, tres conjuntos: `dataset_train`, `dataset_validation`, `dataset_test`.
No se fijan aquí porcentajes porque no existe una decisión tomada al respecto.

Consideraciones específicas para datos transaccionales:

- **Separación temporal, no aleatoria.** Con series temporales, partir al azar filtra
  información del futuro al pasado. El corte debe ser por fecha: entrenar con lo
  anterior, validar con lo posterior.
- **Evitar *data leakage*.** No incluir variables que solo se conocen después del hecho
  que se quiere predecir; por ejemplo, el estado final de la transacción no puede ser una
  feature de un modelo que puntúa en el momento de procesarla.
- **Comparar contra un baseline.** El baseline natural aquí es el propio
  `smartbancs-mock-v1`: si un modelo entrenado no supera a tres reglas fijas, no merece
  el coste operativo de mantenerlo.
- **Evaluar antes de desplegar**, siempre offline y sobre datos no vistos.

## 4.12 Validación del modelo

Un modelo **no se despliega por el hecho de haber sido reentrenado**. Debe superar
criterios fijados antes de entrenar, no elegidos después de ver los resultados.

Las métricas apropiadas dependen del objetivo que se le dé al modelo, que en el MVP no
está definido:

- Si fuese **clasificación** (p. ej. marcar operaciones a revisar): *precision*, *recall*,
  F1 y ROC-AUC, con el umbral de decisión elegido según el coste relativo de falsos
  positivos y negativos.
- Si fuese **scoring de riesgo**: calibración (que un score de 0,7 signifique lo que dice),
  estabilidad del score en el tiempo y métricas de negocio asociadas.

**Ninguna de estas métricas se calcula hoy**: el modelo actual es determinista y no tiene
objetivo predictivo definido ni etiquetas contra las que medirse **[LIM]**.

## 4.13 Registro y versionado del modelo

Propuesta de nomenclatura, coherente con el `modelo` que ya viaja en la respuesta y se
persiste en `analisis_ia`: `smartbancs-model-v1`, `smartbancs-model-v2`, …

Cada versión registrada debería enlazar: **dataset** usado (4.10), **parámetros** de
entrenamiento, **commit de código**, **fecha**, **métricas** de evaluación y **estado**.

Estados conceptuales: `candidate` → `staging` → `production` → `retired`.

**No existe MLflow ni ninguna otra plataforma de registro en el repositorio** **[LIM]**.
Una plataforma de *model registry* sería una evolución razonable, pero el esquema anterior
puede sostenerse inicialmente con una tabla y convenciones.

Un detalle favorable del MVP: como el nombre del modelo se guarda en cada fila de
`analisis_ia`, ya es posible saber **qué versión produjo cada análisis**. Esa es la base
de cualquier comparación entre versiones.

## 4.14 Despliegue del modelo

El desacople actual hace esto sencillo: el modelo vive dentro del proceso consumidor y su
salida se persiste; **ningún otro servicio importa el modelo**.

Propuesta:

- **Despliegue independiente**: actualizar la imagen de IA sin tocar
  `servicio_transacciones` ni su ciclo de vida.
- **Canary**: dirigir una fracción del tráfico de eventos a la versión nueva, comparando
  su distribución de scores contra la versión en producción.
- **Shadow testing**: ejecutar la versión nueva **en paralelo** sin que su resultado sea
  el publicado, y comparar. Requiere poder almacenar dos análisis por transacción, lo que
  hoy impide el `UNIQUE (transaccion_id)` de `analisis_ia`: habría que añadir la versión
  del modelo a la clave.
- **Cambio de versión sin reconstruir el flujo**: el contrato de entrada y salida
  permanece; cambia el contenido de `modelo`, `score`, `nivel` y factores.

## 4.15 Rollback

Rollback **del modelo**, que no tiene nada que ver con el rollback de una transferencia
financiera: revertir un modelo **no deshace dinero**, solo cambia qué análisis se produce
a partir de ese momento. Los análisis ya emitidos permanecen, con su versión registrada.

Para que sea viable hace falta:

1. **Identificar la versión activa** en cualquier momento (campo `modelo` y registro de
   versiones).
2. **Volver a la versión anterior** redesplegando la imagen o cambiando la versión
   configurada, sin reentrenar nada.
3. **Mantener trazabilidad**: cada fila de `analisis_ia` conserva con qué versión se
   generó, así que el corte es auditable.
4. **Comparar métricas antes y después**, agrupando por versión de modelo.

## 4.16 Data drift

**Definición:** la distribución de los datos de entrada cambia respecto a la del dataset
con el que se entrenó el modelo. El modelo no se ha movido; el mundo sí.

Ejemplos aplicados a SmartBancs, con variables que **sí existen** hoy:

- **Distribución de montos**: si el importe típico se desplaza, los tres tramos fijos del
  modelo actual (100 y 1000) dejan de separar lo que separaban.
- **Frecuencia de transacciones** por cuenta y por hora.
- **Proporción de divisas**, si el sistema deja de ser mayoritariamente USD.
- **Proporción de estados**: un aumento de `RECHAZADA` o de motivos concretos de rechazo.
- **Comportamiento del saldo**: distribución de `saldo_resultante` tras los movimientos.
- **Efectos de calendario**: picos de quincena y fin de mes por pago de nóminas, o
  estacionalidad; un modelo entrenado solo con datos de una parte del mes ve otra
  distribución en la otra.

Los ejemplos sobre variables propuestas (4.9) solo aplicarían una vez existan.

## 4.17 Baseline de referencia

Comparar contra "el histórico" sin más no sirve: si la referencia se mueve con los datos,
el drift nunca se detecta.

Propuesta: la referencia es el **dataset de entrenamiento aprobado** de la versión que
está en producción, congelado y versionado (4.10). Contra él se compara la **ventana
productiva reciente**. Cuando se promueve un modelo nuevo, la referencia se sustituye por
su dataset de entrenamiento, y el cambio queda registrado.

## 4.18 Ventanas de monitoreo

Propuesta de partida, no regla absoluta:

- **Diaria**: alerta temprana sobre desviaciones bruscas.
- **Semanal**: análisis con menos ruido, suficiente para distinguir tendencia de
  fluctuación.
- **Mensual**: tendencias de fondo y efectos estacionales.

El tamaño real debe calibrarse con el volumen y el comportamiento del negocio: con poco
volumen, una ventana diaria genera falsas alarmas por ruido estadístico; con mucho, una
ventana mensual detecta tarde.

## 4.19 Métricas de data drift **[PROD]**

Ninguna está implementada. Técnicas aplicables:

| Tipo de variable | Técnicas |
|---|---|
| Numéricas (`monto`, `saldo_resultante`, frecuencias) | PSI (*Population Stability Index*), prueba de Kolmogorov-Smirnov, comparación de media y mediana, desplazamiento de percentiles (p50, p90, p99) |
| Categóricas (`divisa`, `tipo`, `estado`, `razon_fallo`) | PSI sobre categorías, divergencia de Jensen-Shannon, cambio en las proporciones por categoría |

Complemento útil y barato: vigilar la **distribución del score y del nivel** que produce el
modelo. Si el reparto entre `BAJO`, `MEDIO` y `ALTO` se desplaza sin que nada más lo
explique, es una señal aunque no se calcule ningún índice.

## 4.20 Umbrales

No se fijan umbrales numéricos en este documento porque **no existe una decisión tomada
por el equipo ni datos reales sobre los que calibrarlos**.

La formulación correcta es: *un PSI que crece de forma sostenida por encima del umbral
definido por el equipo dispara una investigación*. El umbral se calibra observando la
variación natural del sistema en periodos sin incidentes; lo que en un sistema es ruido,
en otro es señal.

Lo que **no** debe escribirse: "PSI > 0,2 obliga a reentrenar". Un umbral inventado se
convierte en un procedimiento automático sobre una base falsa.

## 4.21 Procedimiento ante drift detectado

El drift **no implica reentrenar automáticamente**. Secuencia propuesta:

1. **Alerta** con la variable, la ventana y la magnitud.
2. **Validar la calidad de los datos**: ¿es un cambio real o datos corruptos?
3. **Descartar un fallo del pipeline**: un cambio de formato en el origen, un ETL a medias
   o un despliegue reciente producen "drift" que en realidad es un error.
4. **Analizar qué variables** se desplazaron y en qué dirección.
5. **Comparar el desempeño** del modelo en esa ventana, si hay forma de medirlo (4.23).
6. **Determinar el impacto**: puede haber drift en una variable que el modelo apenas usa.
7. **Decidir si reentrenar**, que es una decisión con coste y riesgo, no un reflejo.
8. **Validar la nueva versión** contra los criterios de 4.12.
9. **Desplegar** con canary o shadow (4.14).
10. **Monitorear** el efecto del cambio.

## 4.22 Model drift frente a data drift

| | Data drift | Model drift (degradación de desempeño) |
|---|---|---|
| Qué cambia | La distribución de las entradas | La calidad de las predicciones respecto al objetivo |
| Cómo se detecta | Comparando distribuciones contra el baseline | Midiendo el desempeño contra resultados reales conocidos |
| Necesita etiquetas | No | **Sí** |

Pueden darse por separado, y eso es lo relevante:

- **Data drift sin model drift**: los montos suben por inflación, pero el modelo sigue
  ordenando bien el riesgo relativo.
- **Model drift sin data drift**: las entradas se ven idénticas, pero el comportamiento
  fraudulento cambia de táctica y el modelo deja de detectarlo.

Por eso hay que vigilar ambas cosas: el data drift es una **señal temprana y barata**; el
model drift es el **daño real**, pero se detecta más tarde y exige etiquetas.

## 4.23 Ground truth

Para medir desempeño real hace falta conocer después el resultado correcto: si una
operación marcada como `ALTO` era efectivamente problemática, o si una marcada como `BAJO`
resultó ser fraude.

**El MVP no tiene ground truth [LIM]:** no existe ninguna tabla, campo ni proceso donde se
registre el resultado verificado de una operación. No hay realimentación de analistas, ni
confirmaciones de fraude, ni reclamaciones de clientes.

Consecuencia honesta: **hoy no es posible calcular precision, recall ni ninguna métrica de
desempeño**, y afirmar lo contrario sería inventar. Lo único medible sin etiquetas es la
operación (latencia, errores) y la distribución de los scores.

**[PROD]:** habilitar ground truth requiere un circuito de realimentación —revisión por
analistas, confirmación de incidentes, resolución de reclamaciones— cuyo resultado se
almacene asociado a `transaccion_id`. Sin ese circuito, cualquier modelo entrenado queda
sin forma de ser evaluado en producción.

## 4.24 Monitoreo del modelo

Lo que **ya existe [MVP]**: `smartbancs_ia_analisis_total{resultado}`,
`smartbancs_ia_duracion_seconds`, `smartbancs_ia_mensajes_total{resultado}` y
`smartbancs_ia_persistencia_duracion_seconds`, expuestas por `consumidor_ia` en el puerto
9102 y recolectadas por Prometheus. La API `servicio_ia` **no expone métricas ni trazas**
**[LIM]**. Detalle general en [05_observabilidad.md](05_observabilidad.md).

Lo que **habría que añadir [PROD]**, separado por naturaleza:

| Plano | Métricas |
|---|---|
| **Operacional** | Latencia de inferencia (ya existe), throughput, errores, timeouts, profundidad y antigüedad de la cola, CPU y memoria del proceso |
| **Datos** | Índices de drift por variable, porcentaje de nulos, distribuciones de entrada |
| **Modelo** | Distribución de `score` y `nivel`, estabilidad entre ventanas, desempeño cuando exista ground truth |
| **Negocio** | Utilidad real de la recomendación: tasa de aceptación o de acción sobre la recomendación, si llegara a existir ese circuito |

## 4.25 Gestión del consumo de recursos

El reto pregunta explícitamente cómo se gestiona el consumo de recursos.

**Lo que hay hoy [MVP]:**

- **Separación de procesos**: `consumidor_ia` es un contenedor distinto del servicio
  transaccional; su carga de CPU no compite dentro del mismo proceso.
- **`prefetch_count=1`**: el consumidor no acumula mensajes sin confirmar; toma uno,
  lo procesa y confirma.
- **Una conexión de base de datos por mensaje**, abierta y cerrada en el ciclo.
- **Coste de inferencia despreciable**: el modelo son tres comparaciones y una suma.

**Lo que NO hay [LIM]:**

- **Ningún límite de CPU ni de memoria** en `docker-compose.yml`: ningún servicio declara
  `deploy.resources`, `mem_limit` ni `cpus`. Todos compiten libremente por la máquina.
- **Una sola instancia** de `consumidor_ia`, sin escalado.
- **Ningún límite de profundidad de cola** ni política de descarte.
- **Sin pool de conexiones** en el consumidor: abrir una conexión por mensaje es
  aceptable con el caudal actual y sería lo primero a cambiar al escalar.

**Propuesta [PROD]:** límites explícitos de CPU y memoria por contenedor; número de
consumidores dimensionado por medición, no por intuición; tiempo máximo de inferencia;
umbrales sobre el tamaño de cola; y pool de conexiones en el consumidor si aumenta la
concurrencia.

## 4.26 Backpressure

Si llegan más eventos de los que la IA puede procesar, RabbitMQ absorbe el backlog: esa es
su función y es lo que protege al productor. Pero **una cola que crece sin límite no es
resiliencia, es un fallo diferido**: consume memoria y disco del broker y, al final,
degrada a todos sus usuarios.

Hoy no hay ninguna gestión de esto **[LIM]**. Propuesta:

- **Alertas por profundidad de cola y por antigüedad del mensaje más viejo**, que es la
  métrica que de verdad describe el retraso percibido.
- **Escalado de consumidores** cuando el backlog es sostenido y no un pico.
- **Priorización**: si algún análisis fuese más urgente que otro, colas separadas por
  prioridad en lugar de una única cola FIFO.
- **Degradación controlada**: ante un backlog extremo, es preferible reconocer que el
  análisis se retrasa (o incluso omitirlo para parte del tráfico, dejándolo marcado) antes
  que arrastrar al broker.
- **Batch** donde tenga sentido (4.29).

## 4.27 Escalabilidad de la IA

Al estar desacoplada, `consumidor_ia` puede escalar **horizontalmente** sin tocar
`servicio_transacciones`: varias instancias consumiendo de `cola_ia` se reparten los
mensajes de forma natural, y el *upsert* por `transaccion_id` hace que un reproceso no
duplique filas.

Lo que no debe olvidarse: **escalar consumidores traslada la presión a otro sitio**.

- **Base de datos**: más consumidores, más conexiones concurrentes contra la misma
  instancia de PostgreSQL que atiende las transferencias. Este es el límite práctico más
  cercano en la arquitectura actual.
- **Broker**: más conexiones y canales.
- **Modelo**: si en el futuro es costoso, cada instancia multiplica el consumo de CPU o
  memoria.
- **Infraestructura**: CPU y memoria del host.

Por eso no se escala sin límite: el número correcto de consumidores es el que satura el
recurso más escaso sin comprometer al servicio financiero.

## 4.28 CPU y memoria

Con el modelo actual, el consumo es mínimo. Para un modelo futuro más pesado **[PROD]**:

- **Limitar el número de workers** por instancia y el número de instancias.
- **Fijar límites de memoria** por contenedor, para que un modelo que crece no deje sin
  recursos al servicio financiero en el mismo host.
- **Evaluar GPU solo si el tipo de modelo lo justifica.** Un árbol de decisión o una
  regresión logística sobre estas variables no necesita GPU, y añadirla sería coste y
  complejidad sin beneficio.
- **Aislar recursos**: el principio es que la IA nunca compita con el camino crítico
  financiero por CPU, memoria o conexiones de base de datos.

## 4.29 Timeouts

**Hoy [MVP]:** el consumidor invoca el modelo **en proceso**, no por red, así que no
existe ningún timeout de IA y tampoco haría falta: es una función pura sin E/S. Los
timeouts que sí existen son los de PostgreSQL, y aplican a `svc_banc_user` en el servicio
transaccional (`lock_timeout` 800 ms, `statement_timeout` 1500 ms), descritos en
[02_backend_base_datos.md](02_backend_base_datos.md). **Son cosas distintas y no deben
mezclarse**: aquel timeout protege la latencia de una transferencia; el de IA protegería
el avance del consumidor.

**[PROD]:** si el modelo pasa a ser una llamada de red (servicio de inferencia separado),
esa llamada necesita tiempo máximo explícito. Ante un timeout: registrar el error,
reintentar según la política definida —con DLQ al agotarla— y, en todo caso, **no afectar
a la transferencia ya confirmada**. La consecuencia máxima aceptable de un timeout de IA
es un análisis que no llega.

## 4.30 Inferencia por lotes

No es un requisito, es una opción con un ámbito concreto.

- **No usarla** cuando la recomendación deba estar disponible pronto para cada operación
  individual, que es el caso de uso actual de la interfaz.
- **Sí es adecuada** para analítica sobre periodos cerrados, para recalcular scores de un
  histórico completo tras publicar una versión nueva del modelo, para generar datasets de
  evaluación y para procesos no urgentes.

Con el volumen del MVP, el procesamiento evento a evento es la elección correcta.

## 4.31 Degradación controlada

Es la propiedad más importante del diseño y **ya se cumple [MVP]**: si la IA falla, el
sistema financiero sigue funcionando.

| Situación | Consecuencia real hoy |
|---|---|
| `consumidor_ia` caído | Las transferencias se procesan con normalidad; los mensajes se acumulan en `cola_ia`; los análisis aparecen cuando el consumidor vuelve. |
| RabbitMQ caído | Las transferencias se procesan con normalidad; los eventos se acumulan en `eventos_outbox` como `PENDIENTE`; el worker los publica cuando el broker vuelve. |
| `servicio_ia` (API) caído | Las transferencias y los análisis siguen; solo falla la **consulta** del análisis, y `demo_web` responde `503` en ese endpoint concreto. |
| Fallo al analizar o persistir un mensaje | El mensaje se descarta (`requeue=False`): esa transferencia queda sin análisis de forma permanente **[LIM]**. |

Lo que **nunca** ocurre: una transferencia rechazada por culpa de la IA. No existe ninguna
ruta de código que lo permita, porque el servicio transaccional no conoce al de IA.

## 4.32 Seguridad y datos en el modelo **[PROD]**

Brevemente, ya que la política completa no corresponde a este bloque:

- **Minimizar los datos enviados al modelo.** El MVP ya lo hace, aunque sea por
  simplicidad: solo viajan `id_transaccion`, `monto`, `divisa` y `tipo`. Ninguna
  información personal del titular llega al análisis.
- **Anonimización o seudonimización** de los identificadores cuando los datos salgan del
  entorno operativo hacia el analítico.
- **Control de acceso** diferenciado: la vista de administración expone score, factores y
  modelo; la de usuario solo nivel y recomendación. Esa separación ya existe en el
  contrato.
- **Trazabilidad**: cada análisis conserva qué versión de modelo lo produjo y cuándo.
- **No incorporar información innecesaria** solo porque esté disponible: cada variable
  añadida es una decisión de privacidad, no solo una decisión técnica.

---

## 4.33 Limitaciones del MVP

No son fallos: son el alcance elegido. Lo que el MVP demuestra es el **desacoplamiento, el
contrato, el procesamiento asíncrono, la persistencia y la observabilidad básica** del
componente de IA. Lo que no demuestra es capacidad predictiva.

1. **El modelo es una heurística determinista** (`smartbancs-mock-v1`), sin librerías de
   ML ni modelo entrenado **[LIM]**.
2. **No hay entrenamiento, evaluación ni dataset de entrenamiento** en el repositorio
   **[LIM]**.
3. **No hay registro de modelos** ni versionado más allá del nombre almacenado en
   `analisis_ia.modelo` **[LIM]**.
4. **No hay pipeline de MLOps**: ni orquestación, ni promoción de versiones, ni rollback
   automatizado **[LIM]**.
5. **No hay detección de drift** de ningún tipo **[LIM]**.
6. **No hay ground truth**, por lo que el desempeño real no es medible **[LIM]**.
7. **No hay autoescalado** del consumidor ni límites de recursos declarados **[LIM]**.
8. **Un mensaje que falla se descarta sin DLQ**, y la transferencia afectada queda sin
   análisis de forma permanente **[LIM]**.
9. **La API `servicio_ia` no está instrumentada**: sin métricas Prometheus ni trazas
   OpenTelemetry, a diferencia del consumidor **[LIM]**.
10. **El consumidor comparte la instancia de PostgreSQL** con el servicio transaccional
    **[LIM]**.
11. **El campo `categoria` se calcula pero no se persiste** **[LIM]**.
12. **En el flujo asíncrono el nivel depende solo del monto**, porque el `tipo` es fijo y
    la divisa es siempre USD en los datos del MVP **[LIM]**.
