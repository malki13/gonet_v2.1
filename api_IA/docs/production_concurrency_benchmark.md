# Benchmark de concurrencia OCR real

Este benchmark existe para responder dos preguntas con evidencia:

1. Cuánta concurrencia OCR real soporta el servicio en el host evaluado.
2. Cómo escala al subir `OCR_MAX_CONCURRENCY` bajo CPU y memoria reales.

## Qué mide

Por cada combinación de:

- `OCR_MAX_CONCURRENCY`
- concurrencia cliente

el runner levanta la API real, ejecuta carga contra:

- `/v1/ocr` para OpenAI
- `/v1/ocr-tesseract` para Tesseract

y registra:

- tasa de éxito
- latencia `p95`
- throughput
- RSS máximo del proceso
- CPU promedio y máxima del árbol de procesos

## Script

Archivo:

- [`scripts/benchmark_matrix.py`](/home/bryzcoll/Escritorio/Trabajo/api teseract/scripts/benchmark_matrix.py)

Dependencia auxiliar:

- [`scripts/load_test.py`](/home/bryzcoll/Escritorio/Trabajo/api teseract/scripts/load_test.py)

## Requisitos

- Ejecutar en el mismo host o contenedor que quieres medir
- Para `--mode openai`, exportar `OPENAI_API_KEY` válida
- Para `--mode tesseract`, tener `tesseract` instalado y accesible en PATH
- Usar una muestra realista de comprobante, no un archivo sintético

## Ejemplo OpenAI real

```bash
export OPENAI_API_KEY=...

python scripts/benchmark_matrix.py \
  --mode openai \
  --file "muestras/comprobante-real.jpg" \
  --requests 20 \
  --client-concurrency-levels 1,2,4,8 \
  --ocr-max-concurrency-levels 1,2,4,8 \
  --web-concurrency 1 \
  --queue-timeout-seconds 2 \
  --request-timeout-seconds 120 \
  --min-success-rate 0.99 \
  --max-p95-ms 5000 \
  --max-rss-mb 4096 \
  --output artifacts/openai-concurrency-report.json
```

## Ejemplo Tesseract real

```bash
python scripts/benchmark_matrix.py \
  --mode tesseract \
  --file "muestras/comprobante-real.jpg" \
  --requests 50 \
  --client-concurrency-levels 1,2,4,8 \
  --ocr-max-concurrency-levels 1,2,4,8 \
  --web-concurrency 1 \
  --queue-timeout-seconds 2 \
  --request-timeout-seconds 60 \
  --min-success-rate 0.99 \
  --max-p95-ms 3000 \
  --max-rss-mb 2048 \
  --output artifacts/tesseract-concurrency-report.json
```

## Cómo interpretar el resultado

El script imprime una matriz y además calcula:

- `max_qualified_client_concurrency` por cada valor de `OCR_MAX_CONCURRENCY`

Una corrida queda `qualified=true` sólo si cumple simultáneamente:

- `success_rate >= min_success_rate`
- `p95 <= max_p95_ms`
- `rss_max_mb <= max_rss_mb`

## Cómo redactar la conclusión

Formato recomendado:

- "En el host evaluado, con `OCR_MAX_CONCURRENCY=4`, el servicio soportó hasta 4 solicitudes OCR concurrentes con `success_rate=100%`, `p95=... ms` y `RSS max=... MB`."

- "Al subir `OCR_MAX_CONCURRENCY` de 4 a 8, la latencia `p95` pasó de `...` a `...` y el RSS máximo de `... MB` a `... MB`, por lo que la mejora de throughput fue/no fue proporcional."

## Criterio práctico

No afirmes "soporta 8 concurrentes" sólo porque alguna corrida terminó.

Afírmalo sólo si:

- la corrida quedó `qualified=true`
- el archivo usado representa casos reales
- la prueba se hizo en el mismo tamaño de host que usarás en producción
- no hubo throttling externo relevante de OpenAI durante la medición
