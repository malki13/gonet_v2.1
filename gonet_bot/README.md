# gonet-platform

Monorepo base de GoNet para el orquestador conversacional multiagente.

Este repo concentra la API principal, el enrutamiento conversacional, los agentes de dominio, los adaptadores de integracion y las utilidades operativas necesarias para mantener el flujo actual mientras se retira el stack legacy.

## Que hay en la repo

```text
apps/
  bot_api/      FastAPI principal, rutas HTTP, seguridad y bootstrap
  worker_ocr/   Consumidor local de la cola OCR
  ops/          Scripts de smoke test y carga

packages/
  agents/       Logica de negocio por dominio: sales, support, billing y handoff
  channels/     Entrega de mensajes, media temporal e inbound de Meta
  integrations/ Adaptadores a Redis, Odoo, OpenAI, OCR, SMTP, OTP y servicios externos
  orchestrator/ Router, politicas, estados de sesion y composicion de respuesta
  shared/       Configuracion, schemas, helpers y constantes compartidas

tests/          Pruebas unitarias
docs/           Arquitectura, checklist y guias operativas
```

## Flujo principal

1. `apps.bot_api.main` levanta la aplicacion FastAPI.
2. La entrada valida seguridad runtime, inicializa logging y arranca el scheduler de inactividad.
3. El mensaje entra por `POST /v1/messages`, `POST /v1/webhooks/meta` o el webhook bruto en `POST /`.
4. `packages.orchestrator` decide si la intencion es `sales`, `support`, `billing`, `handoff` o `clarify`.
5. El agente de dominio procesa el caso y devuelve un `OutboundMessage`.
6. `packages.channels.delivery` entrega la respuesta al canal correspondiente.
7. Si hay comprobante, `billing` puede encolar OCR y esperar callback externo en `POST /v1/ocr/callback`.

## Requisitos

- Python 3.11 o superior
- `ffmpeg` para audio
- Redis para sesion compartida, outbox y cola OCR
- Odoo, Meta, SMTP, OpenAI y OCR externo solo si el ambiente los usa

## Configuracion local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Luego define el entorno minimo en `.env`:

- `APP_ENV=local`
- `MOCK_MODE=true`
- `BOT_API_INTERNAL_SECRET` si vas a probar las rutas internas con auth
- `META_APP_SECRET` y `VERIFY_TOKEN` si vas a validar el webhook real
- `REDIS_URL` si quieres sesion compartida en Redis

### Levantar la API

```bash
uvicorn apps.bot_api.main:app --reload --port 8010
```

O con el script instalado por el paquete:

```bash
gonet-bot-api
```

### Audio opcional

```bash
pip install -e ".[dev,audio]"
export AUDIO_ENABLED=true
```

Variables utiles de audio:

- `AUDIO_REPLY_MODE`
- `AUDIO_STT_ENGINE`
- `AUDIO_STT_MODEL`
- `AUDIO_TTS_ENGINE`
- `AUDIO_TTS_VOICE`
- `AUDIO_TTS_VOICE_FEMALE`
- `AUDIO_TTS_VOICE_MALE`
- `AUDIO_TTS_PIPER_BIN`
- `AUDIO_TTS_PIPER_MODEL`
- `AUDIO_FFMPEG_BIN`

## Docker

```bash
docker compose up --build
```

La composicion levanta:

- `redis`
- `bot_api`

El worker OCR local queda disponible con el profile `legacy-local-ocr`:

```bash
docker compose --profile legacy-local-ocr up --build
```

## Endpoints

### Publicos o de integracion

- `GET /health`
- `GET /` para verificacion del webhook de Meta con `VERIFY_TOKEN`
- `POST /` para recibir el webhook bruto de Meta
- `POST /v1/webhooks/meta`
- `POST /v1/ocr/callback`

### Internos

- `POST /v1/messages`
- `POST /send`
- `GET /media/{token}`
- `POST /close`
- `POST /v1/outbound`
- `GET /v1/outbound?session_id=...`

Las rutas internas usan `BOT_API_INTERNAL_SECRET` con `X-Internal-Secret`, `X-API-Key` o `Authorization: Bearer`.

## Variables clave

Consulta `.env.example` para la lista completa. Las mas importantes por area son:

- Core: `APP_ENV`, `LOG_LEVEL`, `PORT`, `MOCK_MODE`, `PUBLIC_BASE_URL`
- Seguridad: `BOT_API_INTERNAL_SECRET`, `META_APP_SECRET`, `VERIFY_TOKEN`
- Sesion: `REDIS_URL`, `MEMORY_TTL_SECONDS`
- Gateway: `URL_ODOO_CHAT`, `TOKEN_WHATSAPP`, `PAGE_ACCESS_TOKEN`, `URL_WPP`, `URL_MSG`
- Audio: `AUDIO_*`
- OCR: `OCR_ASYNC_ENABLED`, `OCR_SERVICE_URL`, `OCR_CALLBACK_SECRET`, `OCR_QUEUE_NAME`
- OpenAI: `OPENAI_API_KEY`, `OPENAI_MODEL`
- Odoo y ERP: `ODOO_*`, `ODOO_JSONRPC_*`
- Soporte: `SMART_TELCOM_*`, `ONU_BASE_URL`
- Billing: `OTP_*`, `FRANCHISE_*`

## Dependencias sensibles

Hay piezas que conviene tratar como parte del contrato operativo del bot:

- `PUBLIC_BASE_URL`: expone `GET /media/{token}` hacia Odoo y otros consumidores externos
- `REDIS_URL`: sostiene sesion compartida, OCR y parte del manejo de media temporal
- `URL_ODOO_CHAT`: gobierna handoff y relay humano
- `ODOO_*` y `ODOO_JSONRPC_*`: se usan para CRM y resolucion de adjuntos desde Odoo
- `TOKEN_WHATSAPP`, `URL_WPP`, `WHATSAPP_MEDIA_TOKEN`: gobiernan la entrega real a WhatsApp
- `CONTACT_PG_DSN` o `DATABASE_URL`: sostienen el `contact_registry`

## Consideraciones operativas

- `SessionState` no es el unico estado persistido del sistema; el bot tambien usa `contact_registry` para `channel_id`, `internal_user`, grupo y continuidad humano/IA.
- Si el ambiente depende de adjuntos, valide tambien `GET /media/{token}` desde una URL publica real.
- Si el ambiente usa Odoo para adjuntos, valide `URL_ODOO_CHAT` y `ODOO_JSONRPC_*`.
- Si el ambiente usa soporte tecnico, valide `SMART_TELCOM_*` antes de asumir que el flujo L1 funciona de extremo a extremo.

## Operacion y pruebas

### Smoke test

```bash
python -m apps.ops.smoke_tests --base-url http://127.0.0.1:8010
```

### Load test rapido

```bash
python -m apps.ops.load_tests --base-url http://127.0.0.1:8010 --requests 50
```

### Tests

```bash
pytest -q
```

## Documentacion adicional

- [Arquitectura y mapa de la repo](docs/arquitectura.md)
- [Entrega operativa del bot](docs/entrega_operativa.md)
- [Checklist de cierre del monorepo](docs/checklist_cierre_monorepo.md)

## Notas operativas

- `MOCK_MODE=true` desactiva la dependencia de integraciones reales para desarrollo local.
- Si `REDIS_URL` esta configurado y Redis no responde, el runtime falla de forma explicita.
- `apps/worker_ocr` es un consumidor local/legacy de la cola OCR; el objetivo final es usar un OCR externo con callback.
- `GET /v1/outbound` es una outbox de desarrollo, no parte del runtime principal del bot.
