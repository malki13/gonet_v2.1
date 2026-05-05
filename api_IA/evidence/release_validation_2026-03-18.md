# Evidencia Tecnica de Validacion

Fecha de ejecucion: 2026-03-18

## Alcance

Se ejecutaron validaciones funcionales, de rendimiento y de control operativo sobre el microservicio `api teseract` para sustentar el estado tecnico actual del servicio OCR.

## Pruebas funcionales

Comando ejecutado:

```bash
pytest -q
```

Resultado:

- 43 pruebas aprobadas.
- 0 fallas.
- Tiempo total: 1.25 s.

Cobertura validada por la suite actual:

- contrato del endpoint `/v1/ocr`
- carga multipart, JSON base64 y binary body
- manejo de errores API
- logging de ciclo de vida de la solicitud
- ejecucion paralela del runtime acotado
- rechazo por saturacion del runtime
- timeout de procesamiento
- validaciones auxiliares de OCR y reglas de consistencia

## Pruebas de rendimiento y estres

Scripts usados:

```bash
python scripts/load_test.py
python scripts/load_test_server.py
```

El harness de carga levanta un servidor local con OCR mockeado para aislar el comportamiento del runtime de cola, concurrencia y timeout.

### Escenario 1: Operacion nominal

Artefacto: `evidence/validation_nominal.json`

Configuracion:

- 40 solicitudes HTTP
- concurrencia cliente 4
- endpoint `/v1/ocr`
- `OCR_MAX_CONCURRENCY=4`
- `OCR_QUEUE_TIMEOUT_SECONDS=2`
- `OCR_REQUEST_TIMEOUT_SECONDS=5`
- `LOAD_TEST_SLEEP_SECONDS=0.20`

Resultados:

- 40/40 respuestas exitosas
- 0 errores HTTP
- 0 errores de aplicacion
- throughput: 19.13 req/s
- latencia p95: 209.73 ms
- latencia p99: 210.20 ms

Observacion principal:

- con concurrencia cliente alineada al limite interno del runtime, el servicio responde de forma estable y sin rechazos

### Escenario 2: Guardrails de saturacion y timeout

Artefacto: `evidence/validation_guardrails.json`

Validaciones incluidas:

- saturacion controlada con `OCR_MAX_CONCURRENCY=2` y `OCR_QUEUE_TIMEOUT_SECONDS=0.1`
- timeout operativo con `OCR_REQUEST_TIMEOUT_SECONDS=0.1`

Resultados principales:

- saturacion: 100 solicitudes, 6 respuestas `200`, 94 respuestas `429`, throughput 123.95 req/s
- timeout: 20 solicitudes, 20 respuestas `504`, throughput 18.88 req/s

Conclusion tecnica del escenario:

- bajo sobrecarga, el runtime rechaza de forma controlada con `429 service_busy`
- cuando el procesamiento excede el umbral configurado, la API corta con `504 processing_timeout`

## Benchmark E2E real pendiente

Se agrego el benchmark reproducible:

```bash
python scripts/benchmark_matrix.py
```

Este runner permite medir OpenAI real y Tesseract real por niveles de `OCR_MAX_CONCURRENCY`, registrando:

- tasa de exito
- latencia `p95`
- RSS maximo
- CPU promedio y maxima

Estado actual:

- el benchmark quedo implementado y documentado
- no se ejecuto en este host con OpenAI/Tesseract reales porque el entorno actual no dispone de `OPENAI_API_KEY` valida ni de binario `tesseract`

## Cambios realizados

- se corrigio la prueba inestable de saturacion en `tests/test_api.py`
- se agrego validacion automatica de `processing_timeout`
- se incorporo el runner `scripts/benchmark_matrix.py`
- se actualizo `scripts/load_test.py` para reutilizacion y salida JSON
- se documento la metodologia en `docs/production_concurrency_benchmark.md`

## Riesgos y observaciones

- la evidencia actual de rendimiento valida el runtime y la API con OCR mockeado; no es una medicion E2E final con OpenAI/Tesseract reales
- en este host no hay insumos suficientes para afirmar cuanta concurrencia OCR real soporta produccion
- para sostener una cifra de concurrencia productiva hace falta ejecutar `scripts/benchmark_matrix.py` en el host objetivo, con credenciales reales, muestras reales y Tesseract operativo

## Recomendacion de salida

- usar la evidencia actual para cerrar validaciones del runtime, saturacion y timeout
- no afirmar aun una capacidad E2E de concurrencia OCR real en produccion sin ejecutar el benchmark de matriz en el ambiente objetivo
- promover con una validacion final del benchmark real antes de fijar capacidad operativa o SLA
