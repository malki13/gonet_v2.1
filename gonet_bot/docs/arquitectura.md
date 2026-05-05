# Arquitectura del monorepo

Este documento describe la estructura real de `gonet-platform`, como fluye un mensaje por la plataforma y donde se debe tocar el codigo para cambiar cada comportamiento.

Si lo que necesitas es el traspaso operativo del bot y no el mapa tecnico del repo, revisa [entrega_operativa.md](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/docs/entrega_operativa.md).

## 1. Objetivo del repo

`gonet-platform` es el reemplazo gradual del stack legacy de bots de GoNet.

Hoy concentra:

- la API principal de entrada
- el orquestador conversacional
- los agentes de dominio
- la entrega a canales
- la capa de integraciones externas
- la persistencia compartida de sesion
- la cola OCR y su callback
- los scripts operativos de validacion

## 2. Mapa de carpetas

### `apps/`

- `apps/bot_api/`
  - FastAPI principal
  - rutas HTTP
  - seguridad de runtime
  - arranque del scheduler de inactividad
- `apps/worker_ocr/`
  - consumidor local de la cola OCR
  - usa `BillingAsyncProcessor`
- `apps/ops/`
  - `smoke_tests.py`
  - `load_tests.py`

### `packages/orchestrator/`

- `service.py`
  - orquestacion principal
  - clasificacion, ruteo, estado y composicion
- `router.py`
  - decide el agente objetivo
- `conversation_classifier.py`
  - clasificacion semantica / heuristica
- `state_machine.py`
  - actualizacion de `SessionState`
- `session_context.py`
  - lectura y escritura de sesion
- `inactivity.py`
  - scheduler de cierre por inactividad
- `response_composer.py`
  - adapta respuestas antes de entregarlas
- `policies.py`
  - reglas duras de handoff o identidad

### `packages/agents/`

- `sales/`
  - flujo comercial
  - capturas CRM
  - agencias
  - geocoding
  - promociones
- `support/`
  - flujo L1 de soporte
- `billing/`
  - flujo de facturacion
  - OCR
  - registro de pago
- `handoff/`
  - derivacion a Odoo Chat
- `contact_*`
  - logica compartida entre billing y support

### `packages/channels/`

- `delivery.py`
  - entrega final por WhatsApp o Messenger
  - si la salida es media de WhatsApp puede subir el binario a Meta antes de enviar el mensaje
- `meta_inbound.py`
  - normalizacion del webhook bruto de Meta
- `media_proxy.py`
  - media temporal y URLs publicas
- `outbound.py`
  - wrapper comun para mensajes salientes
- `whatsapp.py`, `messenger.py`
  - helpers de canal

### `packages/integrations/`

- Redis: `redis_store.py`, `outbox_store.py`, `ocr_queue.py`, `ocr_callback_store.py`
- Odoo: `odoo_chat.py`, `odoo_jsonrpc.py`, `odoo_crm.py`, `contact_registry.py`
- OCR: `ocr_service_client.py`
- Audio: `speech_to_text.py`, `text_to_speech.py`
- Soporte: `smarttelcom.py`, `onu.py`
- Billing: `billing_registration.py`, `billing_payload.py`, `billing_franchise.py`
- Otros: `promotions_api.py`, `agencies_repo.py`, `contract_lookup.py`, `otp_service.py`, `smtp.py`, `openai_client.py`, `geocoder.py`

### `packages/shared/`

- `config.py`
  - settings centralizadas
- `schemas.py`
  - modelos Pydantic compartidos
- `response_planner.py`
  - plantillas de respuesta
- `assistant_persona.py`
  - estilo de respuesta
- `turn_interpreter.py`
  - interpretacion de turnos activos
- `sales_intents.py`
  - analisis de intenciones comerciales
- `identity.py`
  - extraccion de documentos de identidad
- `logging.py`
  - configuracion de logs

## 3. Flujo de ejecucion

### 3.1 Ingreso

`apps/bot_api.main` monta la app FastAPI y registra:

- `health`
- `gateway`
- `internal`
- `meta_webhook`
- `ocr_callback`
- `outbound`

Durante el lifespan:

- valida secretos requeridos en runtime
- configura logging
- arranca el scheduler de inactividad

### 3.2 Recepcion de mensajes

El ingreso puede venir por:

- `POST /v1/messages`
- `POST /v1/webhooks/meta`
- `POST /`

El webhook bruto `POST /` hace:

- verificacion de Meta con `GET /`
- normalizacion de eventos WhatsApp o Messenger
- resolucion de sesion
- coalescencia de mensajes cortos de texto
- derivacion del mensaje al orquestador

### 3.3 Resolucion de sesion

`packages/orchestrator/session_context.py` usa `build_session_store()`:

- Redis si `REDIS_URL` existe
- memoria local si no existe

La sesion se guarda por `session_id` y tambien por `recipient`, para poder recuperarla desde mensajes consecutivos o callbacks.

Ademas del `SessionState`, el runtime usa `packages/integrations/contact_registry.py` para conservar contexto operativo por contacto:

- `internal_user`
- `channel_id`
- `grupo`
- `activo` / `activo_ia`
- `identificacion`

Ese registro es el que permite:

- reanudar el handoff humano
- saber si un contacto sigue atendido por IA o por humano
- reutilizar el canal de Odoo cuando llegan mensajes posteriores
- mantener coherencia entre webhook externo y trafico interno por `/send`

### 3.4 Ruteo

`packages/orchestrator/router.py` y `conversation_classifier.py` deciden:

- `sales`
- `support`
- `billing`
- `handoff`
- `clarify`

Antes de llegar al clasificador semantico, el router corta casos evidentes:

- mensajes demasiado largos
- ruido/repeticion
- solicitud explicita de humano
- adjunto + contexto de pago

### 3.5 Agentes de dominio

- `SalesAgent`
  - califica intencion comercial
  - recoge datos de CRM
  - maneja catalogo y recomendacion
  - consulta agencias y geocoding
- `ContactFlowService`
  - resuelve facturacion y soporte sobre un contrato
  - carga contratos por cedula
  - pide consentimiento para avanzar
  - decide si el flujo sigue como billing o support
- `BillingAgent`
  - envuelve errores del flujo de facturacion y prepara fallback humano
- `SupportAgent`
  - envuelve errores del flujo de soporte y prepara fallback humano
- `HandoffAgent`
  - crea o reanuda handoff en Odoo Chat
  - reenvia mensajes y adjuntos

### 3.6 Entrega

`packages/channels/delivery.py` toma la respuesta y la manda al canal:

- WhatsApp con texto, botonera o media
- Messenger con texto, botones o imagen

Si `DRY_RUN_EXTERNALS=true`, el envio no sale al exterior y solo retorna un resultado simulado.

### 3.7 Media

`packages/channels/media_proxy.py` genera tokens temporales para media local.

`GET /media/{token}` sirve esos archivos.

Esto se usa para:

- adjuntos base64
- media temporal hacia Odoo Chat
- callbacks internos con referencias temporales

En WhatsApp hay dos caminos de entrega:

- si el bot ya tiene una referencia valida de media, puede enviarla como `link`
- si la referencia apunta al proxy local `/media/{token}`, `packages/channels/delivery.py` puede subir primero el binario a Meta y luego enviar el mensaje usando `media_id`

Para los adjuntos dirigidos a Odoo Chat, `packages/integrations/odoo_chat.py` construye el `message` segun el adjunto disponible:

- si hay `base64_data`, genera un token temporal con `build_public_media_url(token)`
- si no hay `base64_data`, reusa `url`

Por eso `PUBLIC_BASE_URL` no es una variable cosmetica: define si Odoo puede descargar o no la media que el bot le publica.

### 3.8 OCR

El flujo OCR de facturacion esta desacoplado.

Entrada:

1. `Billing` recibe un comprobante.
2. Si `OCR_ASYNC_ENABLED=true`, crea un `OCRJob`.
3. `OCRJobQueue` lo encola en Redis.
4. Un consumidor externo o `apps/worker_ocr` lo procesa.
5. El resultado vuelve por `POST /v1/ocr/callback`.
6. `BillingAsyncProcessor` registra el pago o deriva a handoff.

Protecciones del callback:

- `OCR_CALLBACK_SECRET`
- deduplicacion por `OCRCallbackStore`
- lock de procesamiento
- almacenamiento de resultado por TTL

## 4. Responsabilidades por modulo

### Cambiar ruteo

Editar:

- `packages/orchestrator/router.py`
- `packages/orchestrator/conversation_classifier.py`
- `packages/orchestrator/policies.py`
- `packages/orchestrator/state_machine.py`

### Cambiar soporte

Editar:

- `packages/agents/support/service.py`
- `packages/agents/contact_flow.py`
- `packages/agents/contact_support.py`
- `packages/agents/contact_support_utils.py`
- `packages/agents/contact_utils.py`

### Cambiar facturacion

Editar:

- `packages/agents/billing/service.py`
- `packages/agents/contact_flow.py`
- `packages/agents/contact_billing.py`
- `packages/integrations/billing_registration.py`
- `packages/integrations/ocr_service_client.py`
- `packages/integrations/ocr_queue.py`
- `packages/integrations/ocr_callback_store.py`

### Cambiar comercial

Editar:

- `packages/agents/sales/service.py`
- `packages/agents/sales/flow_helpers.py`
- `packages/agents/sales/utils.py`
- `packages/agents/sales/state_helpers.py`
- `packages/agents/sales/recommendation_utils.py`
- `packages/integrations/promotions_api.py`
- `packages/integrations/agencies_repo.py`
- `packages/integrations/geocoder.py`

### Cambiar gateway o canales

Editar:

- `apps/bot_api/routes/gateway.py`
- `apps/bot_api/routes/gateway_dispatch.py`
- `apps/bot_api/routes/gateway_media.py`
- `apps/bot_api/routes/meta_webhook.py`
- `packages/channels/delivery.py`
- `packages/channels/meta_inbound.py`
- `packages/channels/media_proxy.py`

### Cambiar integraciones

Editar:

- `packages/integrations/odoo_chat.py`
- `packages/integrations/odoo_jsonrpc.py`
- `packages/integrations/openai_client.py`
- `packages/integrations/redis_store.py`
- `packages/integrations/outbox_store.py`
- `packages/integrations/speech_to_text.py`
- `packages/integrations/text_to_speech.py`

### Cambiar configuracion

Editar:

- `packages/shared/config.py`
- `.env.example`
- `apps/bot_api/security.py`

## 5. Variables de entorno por area

### Core

- `APP_ENV`
- `LOG_LEVEL`
- `PORT`
- `MOCK_MODE`
- `PUBLIC_BASE_URL`

### Seguridad

- `BOT_API_INTERNAL_SECRET`
- `META_APP_SECRET`
- `VERIFY_TOKEN`

### Sesion y runtime

- `REDIS_URL`
- `MEMORY_TTL_SECONDS`
- `ENABLE_INACTIVITY_SCHEDULER`
- `TIME_INACTIVE_CHAT`
- `TIME_INACTIVE_CHAT_IA`
- `UPDATE_INACTIVE_CHAT`
- `SEND_INACTIVE_CHAT`

### Gateway y canales

- `URL_ODOO_CHAT`
- `TOKEN_WHATSAPP`
- `PAGE_ACCESS_TOKEN`
- `URL_WPP`
- `URL_MSG`
- `WHATSAPP_MEDIA_TOKEN`
- `WHATSAPP_GRAPH_VERSION`
- `DRY_RUN_EXTERNALS`

### Audio

- `AUDIO_ENABLED`
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
- `AUDIO_MAX_SECONDS`

### OCR

- `OCR_ASYNC_ENABLED`
- `OCR_QUEUE_NAME`
- `OCR_QUEUE_BLOCK_SECONDS`
- `OCR_WORKER_POLL_SECONDS`
- `OCR_SERVICE_URL`
- `OCR_SERVICE_TIMEOUT_SECONDS`
- `OCR_CALLBACK_SECRET`
- `OCR_CALLBACK_LOCK_TTL_SECONDS`
- `OCR_CALLBACK_RESULT_TTL_SECONDS`

### OpenAI

- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `OPENAI_RUNTIME_OUTAGE_SECONDS`
- `OPENAI_HUMAN_HANDOFF_ON_RUNTIME_FAILURE`

### Odoo y CRM

- `ODOO_*`
- `ODOO_JSONRPC_*`
- `ODOO_LEAD_MODEL`
- `PROMOTIONS_URL`
- `AGENCIES_PG_DSN`
- `CONTACT_CENTER_LOOKUP_URL`

### Soporte tecnico

- `SMART_TELCOM_*`
- `ONU_BASE_URL`
- `TEMP_NET_EXCLUDE_IDS`

### Billing / OTP / franquicia

- `OTP_*`
- `FRANCHISE_*`

## 6. Contratos HTTP

### Rutas publicas

- `GET /health`
- `GET /`
- `POST /`
- `POST /v1/webhooks/meta`
- `POST /v1/ocr/callback`

### Rutas internas

- `POST /v1/messages`
- `POST /send`
- `GET /media/{token}`
- `POST /close`
- `POST /v1/outbound`
- `GET /v1/outbound`

Todas las rutas internas exigen `BOT_API_INTERNAL_SECRET`.

## 7. Persistencia

### Sesion conversacional

Se guarda en Redis o en memoria segun `REDIS_URL`.

Campos importantes de `SessionState`:

- `current_intent`
- `last_agent`
- `awaiting_field`
- `cedula`
- `selected_contract`
- `human_handoff`
- `history`
- `metadata`

### Registro operativo de contactos

`packages/integrations/contact_registry.py` mantiene una tabla compartida (`usuarios_gonet`) para continuidad operativa entre canales, Odoo y runtime.

No reemplaza `SessionState`; cumple otra funcion:

- conservar `channel_id` e `internal_user`
- distinguir si el contacto esta en IA o en humano
- guardar `identificacion`
- resolver el `session_id` activo cuando el webhook entra con otra referencia temporal

### Outbox

`packages/integrations/outbox_store.py` mantiene mensajes salientes para inspeccion en desarrollo.

### OCR callback

`packages/integrations/ocr_callback_store.py` evita doble procesamiento del mismo `job_id`.

## 8. Observabilidad

La plataforma usa logging estructurado por modulo:

- `gateway`
- `orchestrator`
- `agents.sales`
- `agents.billing`
- `agents.support`
- `channels.delivery`
- `redis_store`
- `ocr_queue`
- `billing_async`

La correlacion basica siempre debe ser:

- `session_id`
- `recipient`
- `channel`

## 9. Dependencias sensibles

Hay piezas que conviene tratar como dependencias operativas de primer orden:

- `PUBLIC_BASE_URL`
  - se usa para exponer `/media/{token}` hacia Odoo y otros consumidores externos
  - si apunta a una URL no accesible, fallan adjuntos y reenvios
- `REDIS_URL`
  - afecta sesion compartida, cola OCR, callback OCR y parte del manejo de media temporal
- `URL_ODOO_CHAT`
  - gobierna el handoff y el relay hacia Odoo Chat
- `ODOO_*` y `ODOO_JSONRPC_*`
  - se usan para resolver adjuntos, descargar `ir.attachment` y trafico CRM
- `SMART_TELCOM_*`
  - condicionan monitoreo y parte del soporte tecnico
- `TOKEN_WHATSAPP`, `URL_WPP`, `WHATSAPP_MEDIA_TOKEN`
  - gobiernan la entrega real a WhatsApp y la carga de media a Meta
- `CONTACT_PG_DSN` o `DATABASE_URL`
  - soportan el `contact_registry` y, con eso, el estado operativo humano/IA

## 10. Consideraciones operativas

Antes de considerar un ambiente listo, conviene verificar:

1. `GET /health` responde correctamente.
2. `PUBLIC_BASE_URL` apunta al dominio publico vigente del bot.
3. `GET /media/{token}` funciona desde fuera del contenedor si el ambiente depende de media temporal.
4. Redis responde si `REDIS_URL` esta configurado.
5. Odoo Chat responde en `URL_ODOO_CHAT`.
6. Si el ambiente usa adjuntos desde Odoo, las credenciales `ODOO_JSONRPC_*` y `ODOO_*` permiten leer `mail.message` e `ir.attachment`.
7. Si el ambiente usa soporte tecnico, `SMART_TELCOM_*` y `ONU_BASE_URL` estan vigentes.

## 11. Limitaciones actuales

- `MOCK_MODE=true` sigue siendo el camino mas seguro para desarrollo local sin integraciones reales.
- `apps/worker_ocr` es un fallback local o legacy.
- El OCR externo real sigue dependiendo de la integracion que consuma la cola compartida.
- Las pruebas unitarias cubren la base funcional, pero no sustituyen un E2E con servicios reales.
- En este checkout, `pytest -q` puede cortar al importar `apps.bot_api.routes.gateway` por una incompatibilidad de FastAPI/Starlette (`Router.__init__()` con `on_startup`). Antes de usar la suite como senal verde, revisa el pin de dependencias del entorno.

## 12. Orden recomendado de cambio

Si quieres modificar algo y no sabes por donde empezar:

1. revisar `packages/shared/schemas.py` para entender el contrato
2. revisar `packages/orchestrator/service.py` para ver el flujo general
3. revisar el agente de dominio correspondiente
4. revisar el adaptador de integracion involucrado
5. revisar la ruta HTTP que expone el comportamiento
6. agregar o ajustar tests en `tests/unit/`
