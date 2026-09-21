# 10. Uso de Inteligencia Artificial durante el desarrollo

## Declaración del uso de inteligencia artificial

Durante el desarrollo del reto técnico se utilizaron herramientas de inteligencia
artificial como apoyo complementario al proceso de análisis, implementación, validación y
documentación.

Las principales herramientas empleadas fueron:

- ChatGPT, incluyendo apoyo mediante Codex.
- Claude, incluyendo Claude Code.
- Gemini.
- NotebookLM.

---

## ChatGPT / Codex

Se utilizó principalmente como apoyo técnico para revisar y mejorar la solución diseñada
previamente.

Su uso se concentró en:

- análisis de alternativas de implementación;
- revisión de la arquitectura propuesta;
- apoyo para mejorar el tratamiento asíncrono de procesos posteriores a la transferencia;
- revisión del uso de workers para desacoplar el procesamiento de IA del flujo
  transaccional principal;
- identificación y corrección de errores;
- apoyo en la creación y revisión de pruebas;
- análisis de resultados de ejecución y pruebas de carga;
- apoyo para estructurar documentación técnica en Markdown.

Las decisiones principales de arquitectura, selección de tecnologías y diseño funcional
fueron definidas por el desarrollador. La herramienta se utilizó como apoyo para
contrastar, revisar y mejorar dichas decisiones.

---

## Claude / Claude Code

Claude y Claude Code se utilizaron principalmente como herramientas de apoyo durante la
implementación y revisión del código.

Su uso incluyó:

- revisión de código existente;
- detección y corrección de errores;
- apoyo en la creación y ajuste de pruebas;
- revisión de contratos entre servicios;
- verificación de consistencia entre implementación y documentación;
- apoyo en la organización y redacción de documentación técnica;
- revisión de la estructura de los archivos Markdown;
- apoyo en el desarrollo y ajuste de la interfaz de demostración del MVP.

Claude Code permitió trabajar directamente sobre el proyecto para revisar distintos
componentes y facilitar tareas de corrección y validación, manteniendo las decisiones de
diseño bajo revisión del desarrollador.

---

## Gemini

Gemini se utilizó principalmente como herramienta de consulta conceptual.

Su uso estuvo orientado a:

- resolver dudas puntuales sobre sistemas bancarios;
- comprender consideraciones habituales de sistemas transaccionales;
- revisar conceptos relacionados con integridad, concurrencia, consistencia y
  procesamiento financiero;
- contrastar conceptos aplicables al diseño de una solución bancaria;
- obtener contexto general sobre prácticas utilizadas en sistemas financieros reales.

Su participación fue principalmente informativa y no como herramienta directa de
implementación.

---

## NotebookLM

NotebookLM se utilizó como apoyo para el desarrollo de la presentación del reto técnico.

Su uso incluyó:

- organización del contenido de la documentación del proyecto como fuente de la
  presentación;
- síntesis de los puntos principales de la arquitectura, decisiones técnicas y
  resultados obtenidos;
- apoyo en la estructuración del orden y la narrativa de la presentación.

El contenido final de la presentación fue revisado por el desarrollador para asegurar
que fuera consistente con la implementación y la documentación del proyecto.

---

## Componentes y actividades en los que se utilizó IA

Las herramientas de IA sirvieron como apoyo principalmente en:

- definición y revisión de arquitectura;
- procesamiento asíncrono mediante workers;
- integración del servicio de IA fuera del camino crítico de la transferencia;
- resolución de errores de implementación;
- creación y revisión de pruebas;
- análisis de pruebas de carga;
- diseño de la demostración funcional del MVP;
- revisión de documentación técnica;
- redacción y estructuración de archivos Markdown;
- consultas conceptuales sobre sistemas bancarios y arquitectura transaccional;
- desarrollo de la presentación del reto técnico.

---

## Validación humana

El uso de inteligencia artificial no sustituyó la toma de decisiones técnicas ni la
validación de la solución.

Las respuestas, propuestas y fragmentos generados con apoyo de IA fueron revisados antes
de incorporarse al proyecto. La validación incluyó:

- revisión manual del código;
- ejecución de pruebas automatizadas;
- ejecución del entorno mediante Docker Compose;
- pruebas de los servicios;
- verificación del comportamiento transaccional;
- revisión de logs, métricas y trazas;
- pruebas de carga;
- contraste entre la documentación y la implementación real.

La arquitectura, las tecnologías seleccionadas y las decisiones finales del proyecto
fueron evaluadas y aceptadas por el desarrollador.

---

## Alcance del uso de IA

La inteligencia artificial se utilizó como herramienta de apoyo para:

- acelerar el análisis;
- detectar errores;
- contrastar alternativas;
- mejorar documentación;
- reforzar conceptos técnicos.

No se utilizó como sustituto de la validación técnica del proyecto ni como fuente única
para tomar decisiones críticas relacionadas con integridad financiera, concurrencia o
arquitectura.
