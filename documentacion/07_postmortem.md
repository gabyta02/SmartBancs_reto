# 7. Gestión de incidentes y post mortem

Este documento define cómo se analiza un incidente **después** de resuelto. El
procedimiento durante el incidente está en [06_incidente_critico.md](06_incidente_critico.md).

Etiquetas: **[MVP]** implementado y verificable en el código · **[PROD]** propuesta no
implementada · **[LIM]** limitación actual.

> **Alcance.** El incidente del reto es **simulado**. Lo que sigue es la **plantilla** y la
> **metodología**, con un ejemplo de causa raíz claramente marcado como hipótesis. No se
> presentan horas, métricas ni resultados reales, porque no ha ocurrido tal incidente.

---

## 7.1 Principios

El post mortem es **sin culpabilización**. Su finalidad es entender, aprender y prevenir la
recurrencia; no establecer responsabilidades individuales.

Tres reglas que sostienen eso en la práctica:

1. **Las personas actúan razonablemente con la información que tienen en ese momento.** Si
   alguien tomó una decisión equivocada, la pregunta es qué información le faltaba o qué
   hacía que esa decisión pareciera correcta.
2. **La causa raíz es del sistema, no de la persona.** "Alguien no vio la alerta" no es
   causa raíz cuando no existen alertas.
3. **Se documenta también lo que no funcionó**, incluidas las hipótesis descartadas. Es la
   parte más útil y la primera que se omite.

Cuándo hacerlo: para cualquier incidente que afecte a transferencias o a la integridad de
datos, con independencia de su duración. **[LIM]**: el MVP no tiene definido un umbral de
severidad ni un proceso formal de post mortem; esto es la propuesta.

---

## 7.2 Plantilla de post mortem

Estructura a completar. Los campos entre corchetes se rellenan con datos reales del
incidente concreto.

### 1. Resumen ejecutivo

Tres o cuatro frases, comprensibles para alguien que no estuvo: qué pasó, a quién afectó,
cuánto duró y qué se hizo. Sin jerga y sin detalles de diagnóstico.

### 2–4. Identificación

| Campo | Contenido |
|---|---|
| **Fecha y hora de inicio** | [momento en que comenzó la degradación, no cuando se detectó] |
| **Duración** | [desde el inicio real hasta el cierre, con la detección marcada aparte] |
| **Severidad** | [según la clasificación acordada] |
| **Servicios afectados** | [p. ej. `servicio_transacciones`, PostgreSQL; y cuáles **no** se vieron afectados] |

Registrar el inicio real y la detección por separado es deliberado: la diferencia entre
ambos —el **tiempo de detección**— es una de las métricas más accionables del post mortem.

### 5–6. Impacto

| Campo | Contenido |
|---|---|
| **Impacto funcional** | [qué dejó de funcionar: transferencias no completadas, latencia, análisis retrasados] |
| **Usuarios afectados** | [número o proporción estimada, y cómo se estimó] |
| **Impacto financiero** | [operaciones no completadas; **verificar explícitamente que no hubo pérdida ni duplicación de dinero**] |
| **Impacto en datos** | [resultado de las comprobaciones de integridad de 6.18] |

En este sistema el impacto financiero debe poder afirmarse con evidencia, no con
confianza: las consultas de integridad de
[06_incidente_critico.md](06_incidente_critico.md#618-validación-de-integridad) son el
respaldo.

### 7. Detección

Cómo se detectó: alerta, dashboard, reporte de usuario. Y el dato incómodo: **cuánto tiempo
pasó entre el inicio y la detección**.

**[LIM] del MVP**: sin alertas automatizadas, la detección depende de observación manual o
de reportes de usuarios, lo que significa que el tiempo de detección será alto por
construcción. Esto no es un hallazgo del incidente: es una carencia conocida de antemano.

### 8. Timeline

Tiempos **relativos** a `T0` (inicio de la degradación), no horas inventadas:

| Momento | Evento |
|---|---|
| T0 | Comienza la degradación |
| T+_ | Primera señal observable [qué métrica] |
| T+_ | Detección [por quién, por qué medio] |
| T+_ | Inicio de la investigación |
| T+_ | Primera hipótesis [cuál] |
| T+_ | Hipótesis descartada [cuál y con qué evidencia] |
| T+_ | Causa identificada |
| T+_ | Mitigación aplicada [cuál] |
| T+_ | Impacto reducido [evidencia] |
| T+_ | Recuperación completa |
| T+_ | Validación funcional e integridad |
| T+_ | Cierre |

Las hipótesis descartadas pertenecen al timeline. Sin ellas parece que el diagnóstico fue
inmediato, y la siguiente persona que lea el documento no aprenderá a descartar.

### 9. Causa raíz

Una sola pregunta: **¿por qué el sistema permitió que esto ocurriera?** No qué falló, sino
qué hizo posible que fallara así.

> ### EJEMPLO DE CAUSA RAÍZ HIPOTÉTICA
>
> *Marcado como hipótesis: no corresponde a ningún incidente real ni a ninguna medición
> de este repositorio.*
>
> Durante el pico de quincena, un volumen inusual de transferencias concurrentes sobre un
> conjunto reducido de cuentas (nóminas desde pocas cuentas origen) aumentó la contención
> de bloqueos de fila sobre `cuentas`. Al alargarse las transacciones, cada una retuvo su
> conexión más tiempo; con 15 conexiones por worker, el pool pasó a rechazar por plazo
> agotado (50 ms), devolviendo 503. Los clientes reintentaron, aumentando la presión sobre
> las mismas filas, lo que produjo `55P03` por `lock_timeout` y algunos `40P01` por
> deadlock.
>
> **Cadena causal**: concentración de operaciones sobre pocas cuentas → contención de
> bloqueos → transacciones más largas → conexiones retenidas → pool agotado → 503 →
> reintentos → más contención.
>
> **Por qué es una hipótesis plausible y no una afirmación**: requeriría confirmarse con
> `smartbancs_db_lock_wait_seconds`, la distribución de `sqlstate` en los logs, las
> cuentas implicadas y `pg_blocking_pids` durante el incidente. Sin esos datos, es una
> narrativa coherente, que no es lo mismo que una causa.

El bucle de realimentación —503 provoca reintentos, que provocan más contención— es lo que
convierte una degradación en un colapso, y es lo que hay que buscar en cualquier análisis
de este tipo.

### 10. Factores contribuyentes

Distintos de la causa raíz: **no la provocaron, pero amplificaron el impacto o retrasaron
la resolución**. Los siguientes están **verificados en el repositorio**, no supuestos:

| Factor | Evidencia | Efecto |
|---|---|---|
| **Sin alertas automatizadas** | Prometheus sin `rule_files` ni `alerting`; sin Alertmanager; dashboard sin alertas | Alarga el tiempo de detección |
| **Métricas clave sin panel** | El dashboard no cubre pool, threadpool, `db_sql_duracion_seconds` ni `http_errores_total` | Alarga el diagnóstico: hay que consultar Prometheus a mano |
| **Sin métricas de recursos** | No hay cAdvisor ni node-exporter; ningún contenedor declara límites | Impide descartar saturación de CPU o memoria |
| **Sin observabilidad de la cola** | Sin exporter de RabbitMQ | El backlog asíncrono no es visible |
| **Sin exporter de PostgreSQL** | Diagnóstico solo manual por `psql` | Depende de que alguien sepa qué consultar |
| **Contadores no agregados entre workers** | 5 workers sin modo multiproceso de `prometheus_client` | Los valores absolutos confunden durante el incidente |
| **Sin limitación de admisión en el borde** | No hay proxy ni rate limiting | Los reintentos del cliente realimentan la saturación |
| **Logs sin centralizar** | Solo `docker compose logs` | Correlacionar entre servicios es manual y lento |
| **Instancia única de PostgreSQL** | Sin réplica ni failover en `docker-compose.yml` | Sin margen ante una caída del motor |
| **Eventos atascados en `PROCESANDO`** | No existe mecanismo de recuperación | Requiere intervención manual tras el incidente |
| **Mensajes de IA descartados sin DLQ** | `basic_nack(requeue=False)` sin cola de descartes | Análisis perdidos de forma permanente |
| **Sin runbook ni simulacros** | No existen en el repositorio | Cada incidente se improvisa |

### 11. Mitigación

Qué se hizo para reducir el impacto mientras el incidente seguía activo, **distinguiendo
temporal de permanente**:

| Acción | Tipo | Resultado |
|---|---|---|
| [p. ej. pausar `consumidor_ia` y `worker_outbox`] | Mitigación temporal | [efecto medido] |
| [p. ej. cancelar una sesión bloqueante] | Mitigación temporal | [efecto medido] |
| [p. ej. ajuste de pool o workers] | Cambio de configuración | [efecto medido; **fecha de reversión o confirmación**] |

Toda mitigación temporal necesita **fecha de reversión o de confirmación explícita**. Una
mitigación que nadie revierte se convierte en arquitectura por omisión.

### 12. Recuperación

Cómo se volvió a la normalidad, en qué orden, y qué se verificó en cada paso (6.16).
Incluir el resultado de la **validación funcional** (6.17) y de las **comprobaciones de
integridad** (6.18).

### 13. Qué funcionó

Sección obligatoria y no ceremonial: si algo protegió al sistema, hay que saberlo para no
desmontarlo después. En este sistema, los candidatos concretos **[MVP]**:

- La **idempotencia** hizo seguros los reintentos de los clientes: ninguna transferencia se
  duplicó pese a los 503.
- La **transacción ACID única** impidió estados financieros a medias.
- El **rechazo rápido del pool** evitó una cola de espera ilimitada.
- El **Transactional Outbox** permitió pausar el procesamiento asíncrono sin perder
  eventos.
- La **instrumentación por capas** permitió distinguir espera por conexión de lentitud de
  la base.

### 14. Qué no funcionó

Lo contrario, con el mismo rigor: herramientas que faltaron, señales que no existían,
hipótesis que costaron tiempo, acciones que no tuvieron efecto, decisiones que hubo que
revertir.

### 15. Lecciones aprendidas

Deben basarse en la evidencia del incidente, no en máximas generales. Formuladas como algo
que el equipo hará distinto, y no como conclusiones que ya se sabían. Ejemplos del tipo de
lección que este sistema puede producir:

- **El pool no se dimensiona de forma aislada.** Su tamaño depende de la duración de las
  transacciones, que a su vez depende de la contención. Ajustarlo sin medir esa duración es
  mover un número.
- **Más concurrencia no es más throughput.** Con contención sobre las mismas filas, añadir
  workers aumenta la competencia y puede reducir el rendimiento total.
- **El bloqueo hay que medirlo, no suponerlo.** `smartbancs_db_lock_wait_seconds` sube
  antes de que aparezca el primer deadlock: es una señal temprana desaprovechada si no
  tiene panel ni alerta.
- **La latencia media oculta el incidente.** El P95 se degrada mucho antes de que la media
  se mueva.
- **La idempotencia es una propiedad operativa, no solo de diseño.** Es lo que permite
  decirle a un usuario "reintente" sin riesgo, y lo que hace seguro cancelar una sesión.
- **Rechazar rápido es una decisión de disponibilidad.** El 503 con `Retry-After` no es un
  fallo: es lo que evita que el sistema acepte trabajo que no podrá completar.

### 16–20. Acciones correctivas

Tabla con **prioridad, responsable (por rol), fecha objetivo y estado**. Propuesta inicial
derivada de los factores contribuyentes verificados:

| # | Acción | Área | Prioridad | Responsable (rol) | Fecha objetivo | Estado |
|---|---|---|---|---|---|---|
| 1 | Configurar Alertmanager con reglas sobre pool agotado, tasa de error, P95, deadlocks y timeouts | Observabilidad | **P0** | Backend / SRE | [fecha] | Pendiente |
| 2 | Añadir al dashboard los paneles de pool, threadpool, `db_sql_duracion_seconds{operacion}` y `http_errores_total` | Observabilidad | **P0** | Backend | [fecha] | Pendiente |
| 3 | Desplegar `postgres_exporter` y exporter de RabbitMQ | Observabilidad | **P1** | Infraestructura | [fecha] | Pendiente |
| 4 | Métricas de recursos por contenedor (CPU, memoria) | Infraestructura | **P1** | Infraestructura | [fecha] | Pendiente |
| 5 | Recuperación de eventos atascados en `PROCESANDO` (devolución a `PENDIENTE` por antigüedad) | Código | **P1** | Backend | [fecha] | Pendiente |
| 6 | DLQ para los mensajes que el consumidor de IA descarta | Código | **P1** | Backend | [fecha] | Pendiente |
| 7 | Limitación de admisión (rate limiting) en el borde, con 429 y `Retry-After` | Infraestructura | **P1** | Infraestructura | [fecha] | Pendiente |
| 8 | Prueba de carga que reproduzca el escenario de quincena y establezca la capacidad real | Operativa | **P1** | Backend | [fecha] | Pendiente |
| 9 | Runbook de incidente basado en [06_incidente_critico.md](06_incidente_critico.md) | Operativa | **P1** | Equipo | [fecha] | Pendiente |
| 10 | Modo multiproceso de `prometheus_client` para agregar los 5 workers | Observabilidad | **P2** | Backend | [fecha] | Pendiente |
| 11 | Backoff en los reintentos del outbox | Código | **P2** | Backend | [fecha] | Pendiente |
| 12 | Límites de CPU y memoria por contenedor | Infraestructura | **P2** | Infraestructura | [fecha] | Pendiente |
| 13 | Centralización de logs con formato y timestamp homogéneos | Observabilidad | **P2** | Infraestructura | [fecha] | Pendiente |
| 14 | Retención persistente de métricas y trazas | Observabilidad | **P2** | Infraestructura | [fecha] | Pendiente |
| 15 | Réplica de PostgreSQL y procedimiento de failover | Infraestructura | **P2** | Infraestructura / DBA | [fecha] | Pendiente |
| 16 | Definir SLO de latencia y disponibilidad, y alertar por presupuesto de error | Observabilidad | **P2** | Equipo | [fecha] | Pendiente |
| 17 | Procedimiento de escalamiento y guardias | Operativa | **P2** | Equipo | [fecha] | Pendiente |

Criterio de prioridad usado: **P0** acorta la detección o el diagnóstico del próximo
incidente; **P1** evita o acota la recurrencia; **P2** mejora la postura general.

La razón de que las dos primeras sean P0 es concreta: en este sistema el tiempo de
detección y el tiempo de diagnóstico son las dos partes de la duración del incidente que
hoy están peor cubiertas, y ambas se arreglan con trabajo de observabilidad, no de
arquitectura. **Las métricas ya existen y se recolectan; lo que falta es verlas y ser
avisado.**

---

## 7.3 Acciones preventivas por área

Detalle de las acciones anteriores, agrupadas por naturaleza. Todo lo de esta sección es
**[PROD]** salvo donde se indique lo contrario.

### Código

El camino crítico ya aplica buena parte de lo recomendable **[MVP]**: transacciones cortas,
orden determinista de actualización, timeouts definidos en el rol de PostgreSQL, reintentos
acotados con backoff y jitter, e idempotencia. No hay que reescribirlo.

Lo que sí añadiría valor:

- **Recuperación de eventos en `PROCESANDO`**: un evento reclamado por un worker que muere
  se queda ahí para siempre. Devolverlo a `PENDIENTE` por antigüedad cierra el hueco.
- **DLQ en el consumidor de IA**, en lugar de `basic_nack(requeue=False)`.
- **Backoff en los reintentos del outbox**, hoy inexistente.
- **Limpieza del código muerto** en `transferencias.py` (implementación asíncrona
  inactiva): durante un incidente, código que parece activo y no lo es hace perder tiempo.
- **Unificar el significado de `trace_id`** entre servicios, que hoy designa dos cosas
  distintas.

No se propone cambiar el modelo de bloqueo ni la estrategia de reintentos: son adecuados y
cambiarlos bajo la impresión de un incidente sería arreglar lo que funcionó.

### Base de datos

- **Revisar los índices** con evidencia de `pg_stat_statements`, no por intuición. El
  esquema ya tiene índices para las consultas conocidas.
- **Analizar la contención** por cuenta: si el pico se concentra en pocas cuentas origen,
  el problema es de distribución de carga, y ninguna configuración lo resuelve.
- **Revisar `max_connections`** frente al total real de conexiones cuando se ajuste el
  número de workers o de consumidores.
- **Ajustar timeouts con datos**: los valores actuales (`lock_timeout` 800 ms,
  `statement_timeout` 1500 ms) son razonables, pero deben validarse contra la distribución
  real de duración medida.
- **Mantenimiento**: vigilar el crecimiento de `eventos_outbox` y `movimientos`, que solo
  crecen, y definir una política de archivado.

No se proponen valores concretos de *tuning*: fijarlos sin medición sería inventarlos.

### Infraestructura

- **Límites de CPU y memoria por contenedor**: hoy ninguno los declara, así que un proceso
  puede desplazar al resto.
- **Healthchecks** para todos los servicios: hoy solo PostgreSQL tiene uno.
- **Políticas de reinicio**: solo `postgres` y `servicio_transacciones` declaran
  `restart: unless-stopped`; el worker y el consumidor no se reinician solos si mueren.
- **Escalado horizontal**: requeriría eliminar los `container_name` fijos, que hoy impiden
  usar `--scale`, y poner un balanceador delante.
- **Aislamiento de cargas**: separar la base de datos del camino crítico de la que usan los
  procesos asíncronos, o al menos limitar sus conexiones.
- **Alta disponibilidad**: réplica de PostgreSQL y procedimiento de failover probado.
- **Rate limiting** en el borde.

### Observabilidad

Las acciones 1, 2, 3, 4, 10, 13, 14 y 16 de la tabla. El punto de partida es bueno —la
instrumentación interna es detallada y está bien pensada— y la carencia es de **explotación**
más que de **instrumentación**: alertas, paneles y persistencia. Detalle en
[05_observabilidad.md](05_observabilidad.md).

Dos señales que hoy no existen y que este escenario reclama directamente:

- **profundidad y antigüedad de la cola** de RabbitMQ;
- **antigüedad del evento `PENDIENTE` más viejo** en `eventos_outbox`, que es la medida
  real del retraso del pipeline asíncrono.

### Operativas

- **Runbook** derivado de [06_incidente_critico.md](06_incidente_critico.md), con las
  consultas ya escritas y listas para copiar: durante un incidente nadie redacta SQL.
- **Pruebas de carga** que reproduzcan el escenario de quincena —concurrencia alta sobre
  pocas cuentas origen— y no solo carga uniforme. Ver
  [08_pruebas_rendimiento.md](08_pruebas_rendimiento.md).
- **Pruebas de recuperación**: detener PostgreSQL, RabbitMQ o el consumidor y verificar el
  comportamiento esperado. Parte de ese comportamiento ya está diseñado; conviene
  comprobar que ocurre.
- **Simulacros** periódicos del procedimiento de incidente.
- **Revisión de capacidad** antes de cada periodo de pico conocido, que en un sistema
  bancario es predecible: quincena, fin de mes, fechas de pago.
- **Procedimiento de escalamiento** y guardias definidas.

---

## 7.4 Relación con los mecanismos del MVP

Resumen para no confundir lo que el sistema ya hace con lo que falta:

| Mecanismo | Estado | Papel ante un incidente así |
|---|---|---|
| Idempotencia | **[MVP]** | Hace segura la recuperación y los reintentos |
| Transacción ACID única | **[MVP]** | Protege la integridad financiera |
| Timeouts en el rol de PostgreSQL | **[MVP]** | Acotan el daño de una sesión atascada |
| Pool con rechazo rápido | **[MVP]** | Degradación predecible en lugar de colapso |
| Reintentos con backoff y jitter | **[MVP]** | Absorben conflictos transitorios |
| Orden determinista de bloqueo | **[MVP]** | Reduce la probabilidad de deadlock |
| Transactional Outbox | **[MVP]** | Permite pausar lo asíncrono sin perder datos |
| Métricas y trazas detalladas | **[MVP]** | Permiten diagnosticar por capas |
| Triggers de inmutabilidad | **[MVP]** | Impiden corromper el libro mayor al "arreglar" |
| **Alertas** | **[LIM]** | No existen: la detección es manual |
| **Rate limiting** | **[LIM]** | No existe: los reintentos realimentan la saturación |
| **Autoescalado** | **[LIM]** | No existe |
| **HA de PostgreSQL / failover** | **[LIM]** | No existe: instancia única |
| **Runbooks y simulacros** | **[LIM]** | No existen |
| **Centralización de logs** | **[LIM]** | No existe |
| **Métricas de recursos y de cola** | **[LIM]** | No existen |
| **Recuperación de eventos `PROCESANDO`** | **[LIM]** | No existe |

La lectura honesta: el MVP está bien preparado para **no corromper datos** durante un
incidente y razonablemente preparado para **diagnosticarlo**, pero no para **detectarlo
pronto** ni para **recuperarse solo**. Esa asimetría es coherente con el alcance de un MVP,
y es exactamente lo que las acciones P0 y P1 corrigen.
