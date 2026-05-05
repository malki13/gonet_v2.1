# Entrega Operativa del Bot

Este documento sirve para traspasar la operacion de `gonet-platform` a otro equipo sin obligarlo a reconstruir la plataforma leyendo todo el codigo.

No reemplaza [arquitectura.md](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/docs/arquitectura.md).  
`arquitectura.md` explica como esta armado el repo.  
Este documento explica que debe quedar validado para operar el bot, donde mirar cuando algo no responde como se espera y que partes son criticas en produccion.

## 1. Alcance del sistema

El bot cubre estas funciones operativas:

- recepcion de mensajes desde WhatsApp y Messenger
- ruteo conversacional entre `sales`, `support`, `billing`, `handoff` y `clarify`
- continuidad de sesion IA / humano
- handoff y relay hacia Odoo Chat
- consulta de contratos por cédula o RUC
- soporte tecnico L1 con monitoreo de red
- facturacion con OCR asincrono y registro de pago
- entrega de texto, imagen, audio y documentos al canal final

## 2. Componentes que deben existir en ambiente

Para que el bot opere completo, el ambiente debe contemplar:

- `bot_api`
  - API FastAPI principal
  - expone webhook, rutas internas, media temporal y callback OCR
- Redis
  - sesion compartida
  - cola OCR
  - callback OCR
  - soporte de media temporal en runtime
- Odoo Chat
  - handoff humano
  - relay de mensajes del cliente
  - relay de adjuntos cliente -> Odoo
- Odoo JSON-RPC
  - lectura de `mail.message`
  - lectura de `ir.attachment`
  - soporte del flujo Odoo -> cliente
- Meta WhatsApp / Messenger
  - recepcion de mensajes
  - envio de texto y media
- OCR externo
  - consume la cola compartida o recibe trabajo desde el bot
  - responde por callback seguro
- SmartTelcom y ONU
  - monitoreo tecnico de soporte
- Base de contacto / registry
  - continuidad humano/IA
  - `channel_id`, `internal_user`, `identificacion`

## 3. Dependencias sensibles

Estas variables no son accesorias. Si fallan, el bot puede seguir levantando pero quedar roto funcionalmente.

- `PUBLIC_BASE_URL`
  - debe apuntar a la URL publica real del bot
  - se usa para exponer `GET /media/{token}`
  - impacta adjuntos y relay hacia Odoo
- `REDIS_URL`
  - sostiene sesion compartida, OCR y parte del manejo de media temporal
- `URL_ODOO_CHAT`
  - gobierna handoff humano y relay de cliente hacia Odoo
- `ODOO_*` y `ODOO_JSONRPC_*`
  - necesarios para CRM, adjuntos y trafico Odoo -> cliente
- `TOKEN_WHATSAPP`, `URL_WPP`, `WHATSAPP_MEDIA_TOKEN`
  - necesarios para enviar mensajes y media a WhatsApp
- `CONTACT_PG_DSN` o `DATABASE_URL`
  - necesarios para `contact_registry`
- `SMART_TELCOM_*` y `ONU_BASE_URL`
  - necesarios para soporte tecnico automatizado
- `OCR_*`
  - necesarios para el flujo asincrono de comprobantes

## 4. Estado real del sistema

El estado del bot no vive en un solo lugar.

### Sesion conversacional

Se persiste en `SessionState` por [packages/orchestrator/session_context.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/orchestrator/session_context.py) usando [packages/integrations/redis_store.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/integrations/redis_store.py).

Campos que mas afectan comportamiento:

- `current_intent`
- `last_agent`
- `awaiting_field`
- `cedula`
- `selected_contract`
- `human_handoff`
- `metadata`

### Registro operativo de contactos

Se maneja en [packages/integrations/contact_registry.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/integrations/contact_registry.py).

Este registro no reemplaza la sesion. Mantiene:

- `session_id` operativo por contacto
- `identificacion`
- `channel_id`
- `internal_user`
- `grupo`
- estado IA / humano

Sin este registro:

- el handoff humano se rompe con mas facilidad
- el relay Odoo -> cliente pierde continuidad
- el webhook puede abrir sesiones paralelas del mismo contacto

### Estado OCR

Se reparte entre:

- [packages/integrations/ocr_queue.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/integrations/ocr_queue.py)
- [packages/integrations/ocr_callback_store.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/integrations/ocr_callback_store.py)
- [packages/agents/billing_async.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/agents/billing_async.py)

### Media temporal

Se maneja en:

- [packages/channels/media_proxy.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/channels/media_proxy.py)
- [apps/bot_api/routes/gateway_media.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/apps/bot_api/routes/gateway_media.py)

## 5. Flujo operativo resumido

### Ingreso de mensajes

Entrada principal:

- `POST /`
- `POST /v1/webhooks/meta`
- `POST /v1/messages`

Recorrido:

1. el gateway normaliza el mensaje
2. resuelve `session_id`
3. si aplica, coalesce varios mensajes cortos
4. envia el `InboundMessage` al orquestador
5. el orquestador decide agente
6. el agente devuelve `AgentResult`
7. `delivery.py` entrega la respuesta al canal

### Handoff humano

Recorrido principal:

1. el orquestador o un agente decide `handoff`
2. [packages/agents/handoff/service.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/agents/handoff/service.py) prepara resumen y grupo
3. [packages/integrations/odoo_chat.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/integrations/odoo_chat.py) envia el caso a Odoo Chat
4. `contact_registry` marca el contacto como humano

### OCR de facturacion

Recorrido principal:

1. el cliente envia un comprobante
2. billing encola OCR si `OCR_ASYNC_ENABLED=true`
3. el worker o integrador externo procesa
4. el resultado vuelve por `POST /v1/ocr/callback`
5. `BillingAsyncProcessor` registra, responde o deriva

### Odoo -> cliente

Recorrido principal:

1. Odoo llama `POST /send`
2. el gateway intenta resolver media asociada al mensaje o al canal
3. `delivery.py` envia texto o media al canal externo
4. `contact_registry` actualiza continuidad de atencion

## 6. Dónde tocar cada comportamiento

Mapa corto para mantenimiento:

- ruteo e identidad
  - [packages/orchestrator/service.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/orchestrator/service.py)
  - [packages/orchestrator/router.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/orchestrator/router.py)
  - [packages/orchestrator/policies.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/orchestrator/policies.py)
- soporte
  - [packages/agents/contact_flow.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/agents/contact_flow.py)
  - [packages/agents/contact_support.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/agents/contact_support.py)
  - [packages/agents/contact_support_utils.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/agents/contact_support_utils.py)
- facturacion
  - [packages/agents/contact_billing.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/agents/contact_billing.py)
  - [packages/agents/billing_async.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/agents/billing_async.py)
  - [packages/integrations/billing_registration.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/integrations/billing_registration.py)
- comercial
  - [packages/agents/sales/service.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/agents/sales/service.py)
  - [packages/agents/sales/commercial_helpers.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/agents/sales/commercial_helpers.py)
- handoff y Odoo
  - [packages/agents/handoff/service.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/agents/handoff/service.py)
  - [packages/integrations/odoo_chat.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/integrations/odoo_chat.py)
  - [packages/integrations/contact_registry.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/integrations/contact_registry.py)
- gateway y canales
  - [apps/bot_api/routes/gateway.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/apps/bot_api/routes/gateway.py)
  - [apps/bot_api/routes/gateway_dispatch.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/apps/bot_api/routes/gateway_dispatch.py)
  - [apps/bot_api/routes/gateway_media.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/apps/bot_api/routes/gateway_media.py)
  - [packages/channels/delivery.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/channels/delivery.py)
- configuracion
  - [packages/shared/config.py](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/packages/shared/config.py)
  - [/.env.example](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/.env.example)

## 7. Validaciones minimas antes de entregar un ambiente

Checklist operativo minimo:

1. `GET /health` responde bien.
2. `PUBLIC_BASE_URL` apunta al dominio publico vigente.
3. `GET /media/{token}` es accesible desde fuera si el ambiente usa adjuntos.
4. Redis responde si `REDIS_URL` esta configurado.
5. `URL_ODOO_CHAT` responde.
6. `ODOO_JSONRPC_*` y `ODOO_*` permiten leer `mail.message` e `ir.attachment` si el ambiente usa media desde Odoo.
7. WhatsApp puede enviar texto y media.
8. Si el ambiente usa soporte tecnico, SmartTelcom y ONU responden.
9. Si el ambiente usa OCR, el callback seguro funciona.

## 8. Pruebas recomendadas para el traspaso

### Basicas

- mensaje simple de texto por WhatsApp
- handoff manual solicitado por cliente
- soporte con cédula, contrato y consentimiento
- billing con comprobante
- salida desde Odoo hacia WhatsApp con texto
- salida desde Odoo hacia WhatsApp con imagen o audio

### De estado

- continuidad de sesion entre mensajes consecutivos
- continuidad IA / humano usando `contact_registry`
- recuperacion de `session_id` por contacto
- persistencia del seguimiento despues de pedir cédula y consentimiento

### De adjuntos

- cliente -> Odoo con imagen
- cliente -> Odoo con audio o documento
- Odoo -> cliente con imagen
- Odoo -> cliente con audio

## 9. Observabilidad minima esperada

Logs que conviene vigilar:

- `gateway`
- `orchestrator`
- `contact_flow`
- `agents.sales`
- `channels.delivery`
- `integrations.odoo_chat`
- `gateway.media`
- `billing_async`
- `redis_store`

Claves minimas de correlacion:

- `session_id`
- `recipient`
- `channel`

## 10. Limitaciones operativas conocidas

Sin convertir esto en una bitacora de problemas, hay limites que el equipo receptor debe saber:

- si `REDIS_URL` existe y Redis no responde, el runtime falla de forma explicita
- `MOCK_MODE=true` no representa integraciones reales
- el worker OCR local sigue siendo un fallback
- el valor de `PUBLIC_BASE_URL` debe mantenerse vigente si se usa un tunel temporal
- las pruebas unitarias no reemplazan una validacion E2E con servicios reales

## 11. Secuencia sugerida de traspaso

Orden recomendado:

1. revisar [README.md](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/README.md)
2. revisar [docs/arquitectura.md](/home/bryzcoll/Escritorio/Trabajo/gonet-platform/docs/arquitectura.md)
3. revisar este documento
4. validar entorno y dependencias sensibles
5. ejecutar smoke tests
6. ejecutar pruebas unitarias focalizadas
7. correr una validacion E2E real por canal
