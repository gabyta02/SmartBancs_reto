# SmartBancs

SmartBancs es un MVP de arquitectura bancaria orientado al procesamiento de
transferencias financieras, integración asíncrona con servicios externos,
procesamiento de recomendaciones mediante inteligencia artificial y
observabilidad distribuida.

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

git clone https://github.com/gabyta02/SmartBancs_reto.git
cd SmartBancs_reto

## 3.Configurar variables de entorno

Copiar el archivo de ejemplo:

Windows PowerShell
Copy-Item .env.example .env
Linux / macOS
cp .env.example .env

Revisar y completar las variables necesarias antes de levantar el entorno.

El archivo .env contiene configuración local y no debe almacenarse en Git.

## 4. Levantar SmartBancs

Construir y levantar todos los servicios:

docker compose up -d --build

Verificar su estado:

docker compose ps

La infraestructura completa se ejecuta mediante Docker Compose.

## 5. Servicios principales

Una vez iniciado el entorno:

Componente	Dirección
Demo Web	http://localhost:8080
API de transacciones	http://localhost:8000
Servicio Bancs	http://localhost:8001
Servicio IA	http://localhost:8002
Grafana	http://localhost:3000
Prometheus	http://localhost:9090
Jaeger	http://localhost:18086
Flujo principal

El procesamiento de una transferencia sigue de forma general este flujo:

Cliente
   │
   ▼
Demo Web / API
   │
   ▼
Servicio de Transacciones
   │
   ├── validación
   ├── idempotencia
   ├── operación financiera
   ├── persistencia
   └── Transactional Outbox
          │
          ▼
    Worker Outbox
          │
          ▼
       RabbitMQ
          │
          ├───────────────┐
          ▼               ▼
    Consumidor IA     Servicio Bancs
          │
          ▼
     Servicio IA

La transferencia financiera se procesa de forma independiente de las tareas
asíncronas. La IA y las integraciones posteriores no bloquean la respuesta
principal al cliente.

## Demo Web

La aplicación de demostración permite:

visualizar las cuentas disponibles;
seleccionar cuentas mediante número de cuenta;
realizar transferencias;
visualizar el resultado de la transacción;
consultar el análisis generado por IA;
ejecutar el proceso ETL cargando archivos CSV;
acceder a Grafana;
acceder a Jaeger.

Los UUID internos de las cuentas no se muestran al usuario.

## ETL

El proyecto incluye un proceso ETL desarrollado en Python y Pandas.

El ETL:

recibe datos transaccionales sin procesar;
detecta archivos CSV separados por , o ;;
normaliza texto, montos, divisas y fechas;
detecta registros inválidos;
separa registros válidos y observados.

Puede ejecutarse desde línea de comandos:

python -m etl.transformar etl/datos/transacciones_raw.csv

También puede ejecutarse desde la interfaz web cargando un archivo.

Los resultados son separados en:

etl/datos/transacciones_limpias.csv
etl/datos/transacciones_observadas.csv
Pruebas

Ejecutar todas las pruebas automatizadas:

pytest -q

Estado validado durante el desarrollo:

188 passed
Prueba de carga

Las pruebas de carga utilizan k6.

Antes de ejecutarlas se deben crear las cuentas destinadas al escenario de
carga utilizando:

base_datos/seeds/cuentas_carga.sql

El script genera 5.000 cuentas:

LOAD-000001
...
LOAD-005000

Es idempotente y puede ejecutarse varias veces sin duplicarlas.

Posteriormente puede ejecutarse, por ejemplo:

k6 run -e DATASET=LOAD -e RATE=400 -e DURATION=60s tests/carga/carga_transacciones.js

Los valores de RATE y DURATION pueden modificarse según el escenario que se
quiera evaluar.

## Observabilidad

SmartBancs incorpora tres pilares principales de observabilidad.

Métricas

Prometheus recopila métricas relacionadas con:

volumen transaccional;
tiempos de respuesta;
errores;
operaciones de base de datos;
procesamiento de IA.

Grafana permite visualizar estas métricas mediante dashboards.

Trazabilidad distribuida

OpenTelemetry propaga el contexto de traza a través de:

Servicio de Transacciones
        ↓
Worker Outbox
        ↓
RabbitMQ
        ↓
Consumidor IA
        ↓
Servicio IA

Las trazas pueden visualizarse mediante Jaeger.

Logs

Los componentes generan logs estructurados que permiten correlacionar eventos,
transacciones y errores.

Documentación técnica

La documentación completa del proyecto está dividida por área:

Arquitectura del sistema
Backend y base de datos
Integración Bancs y ETL
Inteligencia Artificial
Observabilidad
Incidente crítico simulado
Gestión de incidentes y post mortem
Pruebas y rendimiento
Decisiones técnicas
Uso de Inteligencia Artificial durante el desarrollo
Arquitectura resumida

SmartBancs utiliza una arquitectura híbrida basada en servicios independientes
y comunicación orientada a eventos.

Principales componentes:

FastAPI
Python
PostgreSQL
RabbitMQ
Pandas
Prometheus
Grafana
OpenTelemetry
Jaeger
Docker / Docker Compose

La separación de responsabilidades permite que componentes como IA, ETL y
procesamiento de eventos puedan evolucionar sin formar parte del camino crítico
de una transferencia.

## Detener el entorno

docker compose down

Para eliminar también los volúmenes:

docker compose down -v

La segunda opción elimina los datos persistidos localmente.
