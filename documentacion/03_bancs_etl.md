# 3. Integración Bancs y ETL

Este documento cubre dos temas: la relación entre SmartBancs y el core bancario legado (Bancs), y el proceso ETL de limpieza de datos transaccionales.

Etiquetas usadas:

- **[MVP]** implementado y verificable en el código.
- **[PROD]** propuesta para producción, no implementada actualmente.
- **[LIM]** limitación actual.

---

# Parte A — Integración con el core legado Bancs

## 3.1 Idea general

SmartBancs está diseñado para soportar una carga transaccional superior a la que razonablemente puede asumir un core legado. Por esta razón, Bancs no participa directamente en el camino crítico de una transferencia.

La operación se confirma primero en SmartBancs y el evento asociado queda persistido mediante el patrón **Transactional Outbox**. Posteriormente, un consumidor asíncrono entrega esas actualizaciones a Bancs a un ritmo controlado.

De esta forma, RabbitMQ funciona como amortiguador entre la velocidad de entrada de SmartBancs y la capacidad real del sistema legado. Si SmartBancs recibe un pico elevado de transacciones, Bancs no recibe ese mismo pico de forma directa: los eventos permanecen temporalmente en la cola y son consumidos al ritmo que el core pueda soportar.

En el MVP se construyó el simulador de Bancs, el cliente HTTP y la infraestructura de outbox, pero el flujo hacia Bancs todavía no se encuentra conectado. Esta separación se mantiene explícita en el documento para distinguir entre lo implementado y el diseño planteado para producción.

---

## 3.2 Estado real de Bancs en el MVP

El MVP incluye un **servicio mock** que representa el core legado. La integración completa con el pipeline de eventos **no está implementada**: se plantea como estrategia de producción en las siguientes secciones.

Lo que existe actualmente:

| Aspecto | Estado |
|---|---|
| Servicio Bancs | Sí. Existe un servicio FastAPI que simula el core legado. |
| Endpoint de salud | `GET /health`. |
| Endpoint de eventos | `POST /eventos`. |
| Payload | Incluye identificador del evento, transacción, tipo y carga útil. |
| Persistencia | No persiste información. |
| Reglas de negocio | No aplica reglas reales del core. |
| Integración con el flujo transaccional | No existe todavía. |
| Cliente HTTP | Existe un cliente preparado para enviar eventos a Bancs. |
| Uso del cliente HTTP | Actualmente no se invoca desde el flujo de negocio. |
| Configuración de URL | Existe configuración de `BANCS_URL`. |
| Red Docker | El servicio se encuentra en la misma red del resto de componentes. |
| Pruebas específicas | No existen pruebas dedicadas para la integración completa. |

Resumen: la integración se encuentra parcialmente preparada. Existe el receptor, el cliente y la configuración necesaria, pero aún falta el consumidor dedicado que conecte los eventos con Bancs.

El mock tampoco implementa métricas, persistencia, idempotencia ni reglas contables reales **[LIM]**.

---

## 3.3 Requisito del reto

El requisito consiste en diseñar el flujo de datos entre la nueva aplicación y el core legado Bancs, detallando cómo se actualizarían los saldos sin saturar el sistema legado.

La propuesta se basa en desacoplar completamente el procesamiento transaccional en línea de la actualización del core.

La estrategia utiliza:

- Transactional Outbox.
- RabbitMQ.
- Consumidor dedicado para Bancs.
- Limitación de tasa.
- Reintentos controlados.
- Idempotencia.
- Circuit breaker.
- DLQ.
- Reconciliación posterior.
- Neteo o batching cuando las reglas del core lo permitan.

---

## 3.4 Estrategia propuesta para Bancs [PROD]

```text
Cliente
  │
  ▼
servicio_transacciones                         ← camino crítico [MVP]
  │
  │ PostgreSQL:
  │ saldo operativo
  │ movimientos
  │ estado
  │ eventos_outbox
  │
  ▼
COMMIT ──────────────────────────────────────► respuesta al cliente
  │
  │ fuera del camino crítico
  ▼
worker_outbox
  │
  ├────► RabbitMQ · cola_ia ─────► consumidor_ia        [MVP]
  │
  └────► RabbitMQ · cola_bancs                         [PROD]
                     │
                     ▼
              consumidor_bancs
              ┌──────────────────┐
              │ rate limiting    │
              │ neteo / batching │
              │ idempotencia     │
              │ circuit breaker  │
              │ reintentos       │
              └────────┬─────────┘
                       ▼
                     Bancs
                       │
                       ▼
               reconciliación
                       │
                       ▼
               SmartBancs / alertas
```

Principios del diseño:

1. **Ninguna llamada síncrona a Bancs por transferencia.**  
   El cliente nunca espera la respuesta del core legado.

2. **Bancs fuera del camino crítico.**  
   La latencia o indisponibilidad del core no bloquea el procesamiento local.

3. **Desacople por eventos.**  
   La transferencia y el evento de salida se registran dentro de la misma transacción.

4. **RabbitMQ como amortiguador.**  
   La cola absorbe diferencias de velocidad entre SmartBancs y Bancs.

5. **Consumidor Bancs dedicado.**  
   Toda la protección del core se concentra en un único punto controlado.

6. **Consistencia eventual entre sistemas.**  
   SmartBancs puede haber confirmado una operación mientras Bancs todavía no la refleja.

---

## 3.5 Consideración del Transactional Outbox

Actualmente `eventos_outbox` representa el progreso hacia un único destino.

Si el mismo evento debe publicarse hacia IA y Bancs, se requiere separar el seguimiento de entrega.

Opciones posibles para producción:

- insertar una fila de outbox por cada destino;
- crear una tabla de entregas por destino;
- publicar a un exchange con múltiples colas y delegar parte del seguimiento a RabbitMQ.

Una opción robusta sería almacenar el estado de entrega por destino:

```text
evento_outbox
   │
   ├── entrega IA
   │
   └── entrega Bancs
```

Esto evita marcar un evento como `PUBLICADO` cuando solo uno de los dos consumidores lo haya recibido.

---

## 3.6 Actualización de saldos y modelo de consistencia

### Estado implementado [MVP]

SmartBancs actualiza el saldo local dentro de la misma transacción ACID que registra los movimientos, estado y evento de outbox.

Cuando el cliente recibe la respuesta exitosa:

- el saldo local ya fue actualizado;
- los movimientos ya fueron registrados;
- el evento ya existe en el outbox.

Dentro de SmartBancs existe consistencia fuerte.

Actualmente Bancs no se sincroniza con estas operaciones **[LIM]**.

### Fuente de verdad y saldo operativo [PROD]

En producción se distinguirían dos responsabilidades.

**Bancs se mantendría como sistema de registro del entorno bancario legado**, mientras que SmartBancs mantendría un **saldo operativo local**, optimizado para procesar transferencias en tiempo real sin depender de la disponibilidad inmediata del core.

El saldo operativo se cargaría inicialmente desde Bancs y posteriormente evolucionaría con las operaciones procesadas por SmartBancs.

Las actualizaciones generadas localmente se enviarían a Bancs de forma asíncrona mediante la cola dedicada.

En sentido contrario, SmartBancs realizaría procesos controlados de actualización y reconciliación contra Bancs, preferentemente:

- por lotes;
- por ventanas de tiempo;
- fuera de los periodos de mayor carga del core.

Durante este intervalo existe **consistencia eventual** entre ambos sistemas.

Una diferencia de saldo no debería corregirse automáticamente sin determinar previamente su causa.

---

## 3.7 Flujo Bancs → SmartBancs [PROD]

La integración no debe limitarse al flujo SmartBancs → Bancs.

También debe existir un mecanismo controlado para:

- cargar saldos iniciales;
- consultar operaciones aplicadas;
- detectar transacciones faltantes;
- comparar saldos;
- identificar discrepancias.

Este flujo no forma parte del camino crítico de una transferencia.

Una estrategia posible es:

```text
Bancs
  │
  │ exportación / consulta por ventana
  ▼
proceso de reconciliación
  │
  ├── compara operaciones
  ├── compara saldos
  ├── detecta eventos faltantes
  └── genera alertas
  │
  ▼
SmartBancs
```

No se plantea consultar Bancs antes de cada transferencia, porque eso volvería a introducir al sistema legado dentro del camino crítico que la arquitectura intenta proteger.

---

## 3.8 Protección del core legado

### Mecanismos ya presentes o parcialmente presentes [MVP]

La arquitectura actual ya contiene elementos que sirven como base:

- RabbitMQ como cola intermedia.
- Confirmación manual de mensajes.
- `prefetch_count` limitado en consumidores.
- Procesamiento por lotes pequeños en el worker de outbox.
- Reintentos acotados.
- Timeout HTTP configurado para Bancs.
- Procesamiento asíncrono fuera del camino crítico.

### Protección recomendada [PROD]

| Técnica | Aplicación propuesta |
|---|---|
| **Rate limiting** | El consumidor Bancs envía como máximo el caudal que el core soporte. |
| **Concurrencia limitada** | Se define un número pequeño de consumidores y conexiones simultáneas. |
| **Backpressure** | Si la cola crece, se mantiene el límite de salida en vez de aumentar la presión sobre Bancs. |
| **Neteo por cuenta** | Movimientos de una misma cuenta pueden consolidarse en un ajuste neto cuando el contrato del core lo permita. |
| **Batching** | Si Bancs admite operaciones por lote, varios eventos pueden viajar en una misma llamada. |
| **Circuit breaker** | Si Bancs empieza a fallar, se suspenden temporalmente las llamadas. |
| **DLQ** | Los eventos que agotan reintentos se envían a una cola de fallidos. |
| **Ventanas de sincronización** | La reconciliación masiva puede ejecutarse en horarios de menor carga. |
| **Limitador distribuido** | Si existieran varias réplicas del consumidor, Redis podría usarse únicamente para compartir el estado del rate limiter. |

RabbitMQ continúa siendo el mecanismo de mensajería. Redis no reemplaza la cola.

---

## 3.9 Neteo por cuenta [PROD]

El neteo permite reducir la cantidad de llamadas hacia Bancs cuando múltiples movimientos afectan a la misma cuenta dentro de una ventana corta.

Ejemplo conceptual:

```text
Cuenta 1001

+100
-20
-10
+50
────
+120 neto
```

En lugar de enviar cuatro actualizaciones, podría enviarse un único ajuste de `+120`.

Sin embargo, esta optimización solo puede aplicarse si:

- Bancs permite recibir ajustes agregados;
- las reglas contables lo permiten;
- no se pierde la trazabilidad de las operaciones individuales;
- SmartBancs conserva qué transacciones formaron parte del ajuste.

Por esta razón, el neteo se considera una optimización opcional y no una obligación del diseño.

Si Bancs necesita conocer cada movimiento individual, se mantiene el envío por evento.

---

## 3.10 Ejemplo ilustrativo de nivelación de carga

Supóngase, únicamente como ejemplo, que SmartBancs recibe:

```text
10 000 transferencias por segundo
durante 5 minutos
```

Esto representa:

```text
10 000 × 300 segundos = 3 000 000 operaciones
```

Supóngase también que Bancs únicamente tolera:

```text
300 actualizaciones por segundo
```

SmartBancs no intentaría enviarle 10 000 operaciones por segundo.

RabbitMQ acumularía temporalmente los eventos y el consumidor mantendría un máximo de 300 actualizaciones por segundo.

Sin optimizaciones:

```text
3 000 000 / 300 = 10 000 segundos
≈ 2,78 horas
```

Este tiempo representa únicamente el drenaje teórico del backlog.

El objetivo no es vaciar la cola lo más rápido posible, sino recuperar la sincronización sin provocar una segunda caída del core.

Por esta razón, incluso después de una indisponibilidad, el rate limiting se mantiene.

Las cifras utilizadas son ilustrativas y no representan una medición real de Bancs.

---

## 3.11 Reintentos

### Estado actual [MVP]

El outbox ya aplica reintentos limitados cuando falla la publicación de eventos.

Sin embargo:

- no existe backoff específico hacia Bancs;
- no existe clasificación de errores específica para el core;
- no existe DLQ dedicada;
- no existe circuit breaker.

### Estrategia propuesta [PROD]

| Elemento | Propuesta |
|---|---|
| Número de intentos | Limitado y explícito. |
| Espera | Backoff exponencial con jitter. |
| Errores temporales | Timeout, 5xx, 429, errores de red. |
| Errores funcionales | 400, 404, 422 u otros que no se solucionen repitiendo la llamada. |
| Duplicado | Un 409 por operación ya aplicada puede tratarse como éxito idempotente. |
| Agotamiento | Enviar a DLQ y generar alerta. |

---

## 3.12 Idempotencia frente a Bancs

### Estado actual [MVP]

El mock actual no persiste operaciones y no evita procesar dos veces el mismo evento.

### Diseño propuesto [PROD]

La entrega asíncrona tiene semántica **at-least-once**, por lo que un mensaje puede repetirse.

Para evitar duplicar movimientos:

1. Cada operación debe llevar una clave de idempotencia estable.
2. Bancs o un adaptador delante del core debe registrar las operaciones ya aplicadas.
3. Debe existir una restricción única por transacción.
4. El consumidor solo confirma el mensaje después de obtener una respuesta correcta.

Para operaciones individuales:

```text
clave_idempotencia = transaccion_id
```

Para batching o neteo:

```text
clave_idempotencia = id_lote
```

Además, SmartBancs debe conservar:

```text
id_lote → transacciones incluidas
```

Esto permite reintentar el lote sin aplicar dos veces el mismo ajuste y mantiene la trazabilidad de cada movimiento.

---

## 3.13 Reconciliación [PROD]

La sincronización operativa y la reconciliación tienen objetivos diferentes.

### Sincronización operativa

Entrega continuamente eventos desde SmartBancs hacia Bancs.

### Reconciliación

Detecta inconsistencias que no pudieron resolverse en el flujo normal.

Comparaciones principales:

| Qué comparar | SmartBancs | Bancs |
|---|---|---|
| Transacciones aplicadas | `transacciones` | operaciones registradas en el core |
| Eventos pendientes | `eventos_outbox` | — |
| Saldos | saldo operativo | saldo del core |

La reconciliación debería detectar:

- evento no entregado;
- evento entregado sin confirmar;
- operación existente en un sistema y no en el otro;
- diferencia de saldo;
- lote parcial;
- mensajes enviados a DLQ.

Acciones posibles:

- reproceso controlado;
- alerta;
- análisis manual;
- registro de auditoría.

Una diferencia de saldo nunca debería corregirse automáticamente sin determinar su causa.

---

## 3.14 Comportamiento ante una caída de Bancs

### Estado actual [MVP]

Como Bancs todavía no participa del flujo real, su caída no afecta las transferencias.

### Diseño esperado [PROD]

1. La transferencia local continúa.
2. El evento queda pendiente.
3. RabbitMQ acumula temporalmente los mensajes.
4. El consumidor aplica backoff.
5. El circuit breaker evita seguir golpeando al core.
6. Se monitorea la profundidad de cola.
7. Se monitorea la antigüedad del mensaje más viejo.
8. Cuando Bancs vuelve, el backlog se drena respetando el límite de tasa.
9. Lo que no pueda recuperarse pasa a DLQ o reconciliación.

El objetivo no es vaciar la cola inmediatamente, sino recuperar la sincronización de forma segura.

---

## 3.15 Observabilidad propuesta [PROD]

La integración con Bancs debería exponer al menos:

- profundidad de `cola_bancs`;
- antigüedad del mensaje más viejo;
- tasa de consumo;
- tasa de error HTTP;
- latencia hacia Bancs;
- estado del circuit breaker;
- número de reintentos;
- tamaño de la DLQ;
- eventos sincronizados;
- eventos pendientes;
- discrepancias detectadas en reconciliación.

Estas métricas permiten diferenciar entre:

- pico de carga normal;
- consumidor lento;
- core degradado;
- caída completa;
- acumulación prolongada;
- error funcional de datos.

---

## 3.16 Trade-offs

### Ventajas

- Latencia del usuario independiente del core legado.
- Bancs queda fuera del camino crítico.
- La cola absorbe picos.
- El consumidor controla el caudal.
- La caída de Bancs no bloquea SmartBancs.
- La idempotencia permite reintentos seguros.
- La reconciliación permite detectar inconsistencias.

### Costos

- Consistencia eventual.
- Mayor complejidad operativa.
- Necesidad de reconciliación.
- Necesidad de gestionar duplicados.
- Necesidad de observabilidad adicional.
- Posible acumulación de backlog.
- Diferencias temporales entre el saldo operativo y el core.

---

# Parte B — ETL

## 3.17 Proceso real del MVP

El ETL utiliza Pandas y procesa archivos de transacciones en modo batch.

El proceso:

1. recibe un archivo;
2. detecta su delimitador;
3. carga los datos;
4. normaliza campos;
5. identifica registros válidos;
6. identifica registros observados;
7. escribe dos archivos CSV.

Resultados:

- `transacciones_limpias.csv`
- `transacciones_observadas.csv`

El ETL no inserta datos en PostgreSQL y tampoco participa del flujo transaccional en tiempo real.

---

## 3.18 Formatos soportados

### CSV [MVP]

El ETL acepta CSV con:

- `,`
- `;`

La codificación utilizada es:

```text
utf-8-sig
```

Esto permite eliminar BOM cuando el archivo proviene de Excel o herramientas de Windows.

Los encabezados son obligatorios.

### XLSX [MVP]

El ETL base no procesa directamente Excel.

La interfaz de demostración convierte la primera hoja de un archivo `.xlsx` a CSV y luego ejecuta el mismo pipeline.

---

## 3.19 Campos críticos

El ETL trabaja con:

- `id_transaccion`
- `monto`
- `divisa`
- `fecha`
- `tipo`

Si falta una columna crítica, el proceso completo falla.

Si la columna existe pero una fila contiene un valor inválido, únicamente esa fila pasa al conjunto de observados.

---

## 3.20 Limpieza y normalización

Las principales reglas son:

| Campo | Tratamiento |
|---|---|
| Texto | `strip()` y conversión de vacíos a `None`. |
| Divisa | Conversión a mayúsculas. |
| Tipo | Conversión a mayúsculas. |
| Monto | Conversión numérica y redondeo a dos decimales. |
| Fecha | Conversión a `YYYY-MM-DD`. |
| Encabezados | Eliminación de espacios externos. |

Limitaciones:

- no existe catálogo de divisas;
- no existe catálogo cerrado de tipos;
- no se valida existencia de cuentas;
- no se valida formato específico del identificador.

---

## 3.21 Monto

El monto se procesa de la siguiente forma:

1. se normaliza el texto;
2. se reemplaza `,` por `.`;
3. se convierte a `float`;
4. se redondea a dos decimales.

Casos:

```text
50,25 → 50.25
```

Un monto negativo se considera inválido posteriormente.

Un monto igual a cero actualmente no se rechaza **[LIM]**.

Los separadores de miles complejos no están soportados correctamente **[LIM]**.

---

## 3.22 Fecha

La fecha se normaliza mediante Pandas.

El ETL acepta formatos mixtos y genera:

```text
YYYY-MM-DD
```

Ejemplos aceptados:

```text
2026-09-19
19-09-2026
2026/09/19
05/08/2026
```

Las fechas inválidas se convierten en registros observados.

No se valida un rango temporal específico **[LIM]**.

---

## 3.23 Duplicados

Los identificadores duplicados se detectan dentro del archivo procesado.

Todas las apariciones del identificador repetido se consideran observadas.

La razón es evitar elegir arbitrariamente cuál fila es correcta.

Actualmente no existe deduplicación contra archivos históricos **[LIM]**.

---

## 3.24 Registros observados

Un registro puede ser observado por:

- `id_transaccion_nulo`
- `monto_nulo`
- `monto_invalido`
- `monto_negativo`
- `divisa_nulo`
- `fecha_nulo`
- `fecha_invalida`
- `tipo_nulo`
- `id_transaccion_duplicado`

Un mismo registro puede acumular varios motivos separados por `;`.

---

## 3.25 Archivos de salida

### Registros válidos

```text
transacciones_limpias.csv
```

Columnas:

- `id_transaccion`
- `monto`
- `divisa`
- `fecha`
- `tipo`

### Registros observados

```text
transacciones_observadas.csv
```

Incluye las mismas columnas más:

```text
motivo_rechazo
```

---

## 3.26 Ejecución

### Línea de comandos

```bash
python -m etl.transformar
```

También puede indicarse un archivo:

```bash
python -m etl.transformar etl/datos/transacciones_raw.csv
```

### Interfaz web

La interfaz permite subir:

```text
.csv
.xlsx
```

El archivo:

- se guarda temporalmente;
- se valida;
- se procesa;
- se elimina al finalizar.

La ejecución web es exclusiva: no permite dos procesos ETL simultáneos.

---

## 3.27 Relación entre ETL e IA

Actualmente no existe integración directa entre el ETL y el servicio de IA.

El ETL:

- limpia archivos;
- genera datasets válidos;
- genera datasets observados.

La IA actual:

- recibe eventos del flujo transaccional;
- procesa información mediante RabbitMQ;
- no se entrena con el resultado del ETL.

En producción, el dataset limpio podría utilizarse para:

- entrenamiento;
- reentrenamiento;
- evaluación;
- generación de features;
- análisis de calidad de datos.

---

## 3.28 Relación entre ETL y Bancs

El ETL y la integración con Bancs resuelven problemas diferentes.

### Integración Bancs

Trabaja con eventos operativos próximos al tiempo real:

```text
SmartBancs → RabbitMQ → Bancs
```

### ETL

Trabaja con archivos históricos o batch:

```text
archivo → limpieza → dataset válido / observado
```

Por esta razón, el ETL no se utiliza como mecanismo principal de actualización de saldos del core.

La reconciliación de Bancs puede utilizar procesos batch, pero conceptualmente pertenece al flujo de sincronización y no al ETL de datos analíticos.

---

## 3.29 Por qué separar válidos y observados

Separar registros válidos y observados permite:

- trazabilidad;
- auditoría;
- análisis de calidad;
- depuración;
- reproceso;
- evitar contaminación de datasets futuros.

El objetivo es evitar descartes silenciosos.

---

## 3.30 Pruebas del ETL

Las pruebas cubren:

- normalización de texto;
- normalización de divisa;
- normalización de tipo;
- normalización de monto;
- normalización de fecha;
- duplicados;
- registros observados;
- columnas faltantes;
- carga web;
- archivos vacíos;
- extensiones inválidas;
- tamaño excesivo;
- XLSX;
- limpieza de temporales.

---

## 3.31 Limitaciones actuales del ETL

1. Procesamiento batch de un solo archivo.
2. Carga completa del archivo en memoria.
3. Validaciones de dominio limitadas.
4. Sin persistencia en PostgreSQL.
5. Sin versionado de datasets.
6. Sin linaje de datos.
7. Duplicados solo dentro del archivo.
8. Ejecución manual.
9. Sin métricas de Prometheus.
10. Salidas CSV con rutas fijas en algunos modos de ejecución.

---

## 3.32 Evolución propuesta del ETL [PROD]

| Área | Propuesta |
|---|---|
| Raw | Conservar el archivo original. |
| Staging | Cargar datos normalizados en tablas temporales. |
| Incremental | Procesar por lotes o ventanas. |
| Versionado | Versionar dataset y reglas. |
| Data Quality | Convertir errores en métricas. |
| Linaje | Registrar archivo y fila de origen. |
| Orquestación | Programar ejecuciones. |
| Particionamiento | Separar por fecha. |
| Deduplicación histórica | Comparar contra datos ya ingeridos. |
| Procesamiento distribuido | Utilizarlo solo cuando el volumen realmente lo justifique. |

---

# Conclusión

La arquitectura propuesta separa claramente el procesamiento transaccional del sistema legado.

SmartBancs procesa la operación con baja latencia, RabbitMQ absorbe la diferencia de velocidades, el consumidor Bancs controla cuánto tráfico recibe el core y la reconciliación detecta cualquier discrepancia posterior.

La idea puede resumirse así:

```text
SmartBancs procesa rápido.
RabbitMQ absorbe el pico.
El consumidor Bancs regula el caudal.
Bancs se actualiza de forma eventual.
La reconciliación verifica la consistencia.
```

El ETL permanece como un proceso separado orientado a limpieza y preparación de datos batch, sin interferir con el flujo transaccional en tiempo real.
