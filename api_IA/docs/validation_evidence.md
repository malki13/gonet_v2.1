# Evidencia de validaciones OCR

Fecha de ejecución: 2026-03-18

## 1. Validación automática

Comando ejecutado:

```bash
pytest -q
```

Resultado:

- 43 tests aprobados
- Cobertura validada sobre contrato API, manejo de errores y límites del runtime

## 2. Cierre de hallazgo de saturación

Hallazgo detectado:

- El test de saturación usaba `queue_timeout_seconds=0.02`, pero el runtime normaliza ese valor a un mínimo de `0.1s`
- La prueba quedaba al borde de una carrera temporal con un trabajo de `0.1s`, por eso era intermitente

Acciones aplicadas:

- El test de cola saturada ahora verifica explícitamente la normalización del timeout mínimo
- La ocupación del worker ya no depende de `sleep(0.1)` sino de una espera controlada por evento
- Se agregó una validación automática adicional de `processing_timeout` para trabajos largos

Referencias:

- [`tests/test_api.py`](/home/bryzcoll/Escritorio/Trabajo/api teseract/tests/test_api.py)
- [`src/ocr_service/services/runtime.py`](/home/bryzcoll/Escritorio/Trabajo/api teseract/src/ocr_service/services/runtime.py)

## 3. Evidencia de rendimiento y estrés

Las corridas siguientes se ejecutaron con:

- Servidor de prueba: [`scripts/load_test_server.py`](/home/bryzcoll/Escritorio/Trabajo/api teseract/scripts/load_test_server.py)
- Generador de carga: [`scripts/load_test.py`](/home/bryzcoll/Escritorio/Trabajo/api teseract/scripts/load_test.py)
- Archivo enviado: `/tmp/ocr-load-test.jpg`
- Modo OCR: mock local para aislar el comportamiento del runtime de cola, concurrencia y timeout

Nota:

- En estas corridas el endpoint `/health` queda degradado por usar `OPENAI_API_KEY=test-key` de prueba. Eso no afecta `/v1/ocr` bajo `load_test_server`, porque el procesamiento OCR está mockeado intencionalmente.

### Escenario A. Rendimiento nominal

Configuración:

```bash
OPENAI_API_KEY=test-key
OCR_MAX_CONCURRENCY=4
OCR_QUEUE_TIMEOUT_SECONDS=2
OCR_REQUEST_TIMEOUT_SECONDS=5
LOAD_TEST_SLEEP_SECONDS=0.20
```

Comando:

```bash
python scripts/load_test.py \
  --url http://127.0.0.1:8765/v1/ocr \
  --file /tmp/ocr-load-test.jpg \
  --requests 40 \
  --concurrency 4 \
  --timeout 5
```

Resultado:

- `completed=40`
- `success=40`
- `status_counts={200: 40}`
- `elapsed_seconds=2.091`
- `throughput_rps=19.13`
- `latency_ms={'avg': 205.63, 'min': 203.04, 'p50': 205.04, 'p95': 209.73, 'p99': 210.2, 'max': 210.5}`

Conclusión:

- Con concurrencia alineada al límite del runtime, el servicio responde de forma estable y sin rechazos.

### Escenario B. Estrés por saturación controlada

Configuración:

```bash
OPENAI_API_KEY=test-key
OCR_MAX_CONCURRENCY=2
OCR_QUEUE_TIMEOUT_SECONDS=0.1
OCR_REQUEST_TIMEOUT_SECONDS=5
LOAD_TEST_SLEEP_SECONDS=0.25
```

Comando:

```bash
python scripts/load_test.py \
  --url http://127.0.0.1:8766/v1/ocr \
  --file /tmp/ocr-load-test.jpg \
  --requests 100 \
  --concurrency 20 \
  --timeout 5
```

Resultado:

- `completed=100`
- `success=6`
- `status_counts={200: 6, 429: 94}`
- `elapsed_seconds=0.807`
- `throughput_rps=123.95`
- `latency_ms={'avg': 121.92, 'min': 103.22, 'p50': 108.28, 'p95': 265.13, 'p99': 327.91, 'max': 327.98}`

Conclusión:

- Bajo sobrecarga, el runtime no se bloquea ni degrada silenciosamente: rechaza de forma controlada con `429 service_busy`.

### Escenario C. Timeout operativo

Configuración:

```bash
OPENAI_API_KEY=test-key
OCR_MAX_CONCURRENCY=2
OCR_QUEUE_TIMEOUT_SECONDS=2
OCR_REQUEST_TIMEOUT_SECONDS=0.1
LOAD_TEST_SLEEP_SECONDS=0.25
```

Comando:

```bash
python scripts/load_test.py \
  --url http://127.0.0.1:8767/v1/ocr \
  --file /tmp/ocr-load-test.jpg \
  --requests 20 \
  --concurrency 4 \
  --timeout 5
```

Resultado:

- `completed=20`
- `success=0`
- `status_counts={504: 20}`
- `elapsed_seconds=1.059`
- `throughput_rps=18.88`
- `latency_ms={'avg': 195.05, 'min': 107.37, 'p50': 204.38, 'p95': 207.66, 'p99': 207.96, 'max': 208.04}`

Conclusión:

- El runtime corta trabajos que exceden el umbral configurado y responde con `504 processing_timeout`.

## 4. Alcance de la evidencia

Esto valida:

- Comportamiento estable del runtime en operación nominal
- Rechazo controlado ante saturación
- Respuesta controlada ante timeout
- No regresión del suite automatizado

Esto no valida todavía:

- Latencia real de OCR con OpenAI y Tesseract en entorno productivo
- Variabilidad de red hacia OpenAI
- Rendimiento sobre documentos reales de distinto tamaño y calidad

Para una evidencia E2E final en preproducción o producción controlada, repetir los mismos escenarios con:

- `OPENAI_API_KEY` real
- muestras reales de comprobantes
- trazas de CPU y memoria del contenedor o instancia
