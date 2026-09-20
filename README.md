# SmartBancs

SmartBancs es un MVP de arquitectura bancaria orientado al procesamiento de transferencias financieras, integración asíncrona con servicios externos, procesamiento de recomendaciones mediante inteligencia artificial y observabilidad distribuida.

La solución fue desarrollada como parte de un reto técnico enfocado en:

- arquitectura de software;
- concurrencia transaccional;
- procesamiento asíncrono;
- integración con un core bancario legado;
- procesos ETL;
- inteligencia artificial;
- observabilidad;
- gestión de incidentes;
- pruebas de rendimiento.

---

# Inicio rápido

## 1. Prerrequisitos

Para ejecutar el proyecto se requiere:

- Git
- Docker
- Docker Compose

Para ejecutar pruebas y scripts fuera de Docker también se recomienda:

- Python 3.12+
- k6 para las pruebas de carga

---

## 2. Clonar el repositorio

```bash
git clone https://github.com/gabyta02/SmartBancs_reto.git
cd SmartBancs_reto
```

## 3. Configurar variables de entorno

Copiar el archivo de ejemplo:

### Windows PowerShell

```powershell
Copy-Item .env.example .env
```

### Linux / macOS

```bash
cp .env.example .env
```

Revisar y completar las variables necesarias antes de levantar el entorno.

El archivo `.env` contiene configuración local y no debe almacenarse en Git.

## 4. Levantar SmartBancs

Construir y levantar todos los servicios:

```bash
docker compose up -d --build
```

Verificar su estado:

```bash
docker compose ps
```

La infraestructura completa se ejecuta mediante Docker Compose.

## 5. Servicios principales

Una vez iniciado el entorno:

| Componente | Dirección |
|---|---|
| Demo Web | http://localhost:8080 |
| API de transacciones | http://localhost:8000 |
| Servicio Bancs | http://localhost:8001 |
| Servicio IA | http://localhost:8002 |
| Grafana | http://localhost:3000 |
| Prometheus | http://localhost:9090 |
| Jaeger | http://localhost:18086 |

---

## Flujo principal

El procesamiento de una transferencia sigue de forma general este flujo:

```text
Cliente / Demo Web
        │
        │ HTTP REST
        ▼
┌─────────────────────────┐
│ Servicio Transacciones  │
│ FastAPI + Python        │
└────────────┬────────────┘
             │
             │ Transacción ACID
             ▼
┌─────────────────────────┐
│ PostgreSQL              │
│                         │
│ cuentas                 │
│ transacciones           │
│ movimientos             │
│ eventos_outbox          │
└────────────┬────────────┘
             │
             │ COMMIT
             ├──────────────────────→ Respuesta al cliente
             │
             │ eventos pendientes
             ▼
┌─────────────────────────┐
│ Worker Outbox           │
└────────────┬────────────┘
             │
             ▼
        ┌──────────┐
        │ RabbitMQ │
        └────┬─────┘
             │
             ▼
┌─────────────────────────┐
│ Consumidor IA           │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│ Servicio IA             │
└─────────────────────────┘
```

La transferencia financiera se procesa de forma independiente de las tareas asíncronas. La IA y las integraciones posteriores no bloquean la respuesta principal al cliente.

---

## Demo Web

La aplicación de demostración permite:

- visualizar las cuentas disponibles;
- seleccionar cuentas mediante número de cuenta;
- realizar transferencias;
- visualizar el resultado de la transacción;
- consultar el análisis generado por IA;
- ejecutar el proceso ETL cargando archivos CSV;
- acceder a Grafana;
- acceder a Jaeger.

Los UUID internos de las cuentas no se muestran al usuario.

---

## ETL

El proyecto incluye un proceso ETL desarrollado en Python y Pandas.

El ETL:

- recibe datos transaccionales sin procesar;
- detecta archivos CSV separados por `,` o `;`;
- normaliza texto, montos, divisas y fechas;
- detecta registros inválidos;
- separa registros válidos y observados.

Puede ejecutarse desde línea de comandos:

```bash
python -m etl.transformar etl/datos/transacciones_raw.csv
```

También puede ejecutarse desde la interfaz web cargando un archivo.

Los resultados son separados en:

```text
etl/datos/transacciones_limpias.csv
etl/datos/transacciones_observadas.csv
```

---

## Pruebas

Ejecutar todas las pruebas automatizadas:

```bash
pytest -q
```

Estado validado durante el desarrollo:

```text
188 passed
```

---

## Prueba de carga

Las pruebas de carga utilizan k6.

Antes de ejecutar la prueba es necesario preparar las cuentas de prueba utilizadas por el dataset de carga.

### 1. Crear las cuentas de prueba

El proyecto incluye el script:

```text
base_datos/seeds/carga.sql
```

Este script genera 5.000 cuentas con formato:

```text
LOAD-000001
...
LOAD-005000
```

Para ejecutarlo desde PowerShell:

```powershell
Get-Content .\base_datos\seeds\carga.sql -Raw |
docker exec -i smartbanks_database `
  psql -U postgres -d smartbanks_db
```

> Si el usuario o el nombre de la base de datos son diferentes en tu entorno, reemplaza `postgres` y `smartbanks_db` por los valores configurados.

El script puede ejecutarse varias veces porque utiliza:

```sql
ON CONFLICT (numero_cuenta) DO NOTHING;
```

por lo que no duplica las cuentas existentes.

Opcionalmente, se puede verificar que las 5.000 cuentas fueron creadas con:

```powershell
docker exec -it smartbanks_database `
  psql -U postgres -d smartbanks_db `
  -c "SELECT COUNT(*) FROM cuentas WHERE numero_cuenta LIKE 'LOAD-%';"
```

El resultado esperado es:

```text
5000
```

### 2. Ejecutar la prueba con k6

Una vez creadas las cuentas:

```powershell
k6 run -e DATASET=LOAD -e RATE=400 -e DURATION=60s tests/carga/carga_transacciones.js
```

Los valores de `RATE` y `DURATION` pueden modificarse según el escenario que se quiera evaluar.


---

## Observabilidad

SmartBancs incorpora tres pilares principales de observabilidad.

### Métricas

Prometheus recopila métricas relacionadas con:

- volumen transaccional;
- tiempos de respuesta;
- errores;
- operaciones de base de datos;
- procesamiento de IA.

Grafana permite visualizar estas métricas mediante dashboards.

### Trazabilidad distribuida

OpenTelemetry propaga el contexto de traza a través de:

```text
Servicio de Transacciones
        ↓
Worker Outbox
        ↓
RabbitMQ
        ↓
Consumidor IA
        ↓
Servicio IA
```

Las trazas pueden visualizarse mediante Jaeger.

### Logs

Los componentes generan logs estructurados que permiten correlacionar eventos, transacciones y errores.

---

## Documentación técnica

La documentación completa del proyecto está dividida por área:

1. [Arquitectura del sistema](docs/01_arquitectura.md)
2. [Backend y base de datos](docs/02_backend_base_datos.md)
3. [Integración Bancs y ETL](docs/03_bancs_etl.md)
4. [Inteligencia Artificial](docs/04_inteligencia_artificial.md)
5. [Observabilidad](docs/05_observabilidad.md)
6. [Incidente crítico simulado](docs/06_incidente_critico.md)
7. [Gestión de incidentes y post mortem](docs/07_postmortem.md)
8. [Pruebas y rendimiento](docs/08_pruebas_rendimiento.md)
9. [Decisiones técnicas](docs/09_decisiones_tecnicas.md)
10. [Uso de Inteligencia Artificial durante el desarrollo](docs/10_uso_inteligencia_artificial.md)

---

## Arquitectura resumida

SmartBancs utiliza una arquitectura híbrida basada en servicios independientes y comunicación orientada a eventos.

Principales componentes:

- FastAPI
- Python
- PostgreSQL
- RabbitMQ
- Pandas
- Prometheus
- Grafana
- OpenTelemetry
- Jaeger
- Docker / Docker Compose

La separación de responsabilidades permite que componentes como IA, ETL y procesamiento de eventos puedan evolucionar sin formar parte del camino crítico de una transferencia.

---

## Detener el entorno

```bash
docker compose down
```

Para eliminar también los volúmenes:

```bash
docker compose down -v
```

> La segunda opción elimina los datos persistidos localmente.
