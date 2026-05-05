# Checklist de cierre del monorepo

Este checklist define lo que falta para pasar `gonet-platform` de una base funcional de migración a un reemplazo completo del stack legacy:

- `gonet_bot`
- `MS Bot Contact`
- `MS Bot Informacion`
- `api teseract`

## Estado actual

Hoy el monorepo ya cubre:

- API principal y orquestador conversacional
- gateway público equivalente a `gonet_bot`
- handoff directo a Odoo Chat desde la monorepo
- cierre de sesiones por inactividad dentro del monorepo
- flujo comercial interno
- facturación interna con cola + callback OCR y registro de pago
- contrato OCR asíncrono por cola + callback hacia servicio externo
- soporte conversacional básico
- cambio de credenciales wifi con OTP
- pruebas unitarias básicas pasando

Hoy el monorepo todavía no cubre completamente:

- soporte técnico L1 profundo
- eliminación total de adapters legacy
- validación E2E real con integraciones productivas
- cutover y despliegue final

## Criterio de salida

El monorepo puede considerarse completo cuando:

- no dependa de módulos legacy internos; el OCR debe quedar como servicio externo formal
- el OCR externo procese los comprobantes reales consumiendo la cola compartida
- soporte, facturación y comercial funcionen end-to-end dentro del monorepo
- las integraciones reales estén validadas
- exista evidencia funcional, de carga y de smoke tests del monorepo
- el tráfico pueda migrarse sin depender del stack anterior

## Checklist funcional

### 1. Gateway y orquestación

- [x] Confirmar que `apps/bot_api` cubre los entrypoints principales del middleware actual
- [ ] Unificar el contrato de entrada para WhatsApp, Messenger e internos
- [ ] Definir el contrato de salida estándar para texto, acciones y adjuntos
- [ ] Validar continuidad de sesión entre turnos para `sales`, `support` y `billing`
- [ ] Confirmar que el router maneja correctamente:
  - comercial
  - facturación
  - soporte
  - handoff humano
  - aclaración cuando la intención es ambigua

### 2. Dominio comercial

- [x] Validar que `sales` ya no necesite fallback a `legacy_info`
- [ ] Completar cualquier brecha restante en:
  - agencias
  - captura CRM
  - geocoding
  - creación de lead
  - handoff comercial
  - catálogo/promociones
- [ ] Validar el flujo completo con Redis y sin pérdida de sesión

### 3. Dominio facturación

- [ ] Confirmar que el flujo cubre:
  - selección de contrato
  - valor pendiente
  - link de cobro
  - recepción de comprobante
  - OCR
  - registro de pago
  - reconexión
  - escalamiento cuando corresponda
- [x] Formalizar la integración OCR externa y eliminar la dependencia de `legacy_ocr`
- [ ] Validar duplicados, fechas fuera de rango y campos faltantes con pruebas reales
- [ ] Validar mensajes finales al cliente para:
  - pago registrado
  - saldo pendiente
  - reconexión solicitada
  - derivación humana

### 4. Dominio soporte

- [ ] Confirmar que el soporte conversacional actual cubre sin botones:
  - sin servicio
  - intermitencias
  - internet lento
  - cambio de nombre/contraseña wifi
  - asistencia humana
- [ ] Completar el L1 técnico faltante si es objetivo de producto:
  - revisión ONU
  - reinicio ONU/router
  - alarmas
  - potencia RX/TX
  - diagnóstico más profundo
- [ ] Decidir explícitamente si esas capacidades quedarán:
  - dentro del monorepo
  - o fuera de alcance para la versión inicial

### 5. OCR externo

- [x] Definir el patrón `cola + callback` con OCR separado
- [x] Definir cómo se encola el trabajo OCR desde `billing`
- [x] Implementar el endpoint `POST /v1/ocr/callback` en la monorepo
- [ ] Validar el consumidor de cola en `api teseract`
- [ ] Validar el callback seguro con `OCR_CALLBACK_SECRET`
- [x] Reemplazar `LEGACY_OCR_URL` por `OCR_SERVICE_URL` en el monorepo
- [ ] Agregar pruebas nominales, saturación y timeout del servicio OCR externo con cola

## Checklist técnico

### 6. Integraciones reales

- [ ] Validar Odoo JSON-RPC real
- [ ] Validar Odoo CRM real
- [ ] Validar entrega directa real a WhatsApp/Messenger
- [x] Integrar `URL_ODOO_CHAT` como canal directo de handoff desde la monorepo
- [ ] Validar `URL_ODOO_CHAT` real en ambiente objetivo
- [ ] Validar `ODOO_CHAT_SEND_URL` solo si se mantiene compatibilidad con handoff externo legado
- [ ] Validar SmartTelcom real
- [ ] Validar ONU real
- [ ] Validar SMTP real
- [ ] Validar Redis real compartido entre monorepo y OCR
- [ ] Validar Postgres OTP real
- [ ] Validar promociones y geocoder reales

### 7. Configuración y secretos

- [ ] Revisar todas las variables del `.env.example`
- [ ] Eliminar variables legacy que ya no se necesiten
- [ ] Confirmar manejo de:
  - `WHATSAPP_MEDIA_TOKEN`
  - Redis
  - Postgres
  - Odoo
  - SmartTelcom
  - SMTP
  - OCR
- [ ] Asegurar que no existan credenciales hardcodeadas en el monorepo

### 8. Persistencia y sesión

- [ ] Confirmar que toda la sesión crítica vive en Redis o DB compartida
- [ ] Confirmar que no haya estado relevante en memoria local del proceso
- [ ] Validar reinicio del servicio sin pérdida de continuidad
- [ ] Validar comportamiento con varias réplicas
- [x] Integrar expiración y cierre por inactividad usando el session store del monorepo

### 9. Observabilidad

- [ ] Definir logs estructurados mínimos por request
- [ ] Añadir correlación por `session_id` y `recipient`
- [ ] Añadir métricas básicas:
  - requests
  - errores
  - tiempos por integración
  - handoffs
  - OCR retries
- [ ] Añadir healthchecks útiles para API y worker

## Checklist de pruebas

### 10. Pruebas automatizadas

- [ ] Cubrir router y orquestador
- [ ] Cubrir `sales`
- [ ] Cubrir `billing`
- [ ] Cubrir `support`
- [ ] Cubrir integraciones mockeadas
- [ ] Cubrir persistencia de sesión
- [ ] Cubrir reintentos y errores

### 11. Pruebas de integración

- [ ] Flujo comercial end-to-end
- [ ] Flujo facturación end-to-end
- [ ] Flujo soporte end-to-end
- [ ] Flujo OTP end-to-end
- [ ] Flujo OCR end-to-end
- [ ] Escalamiento a humano end-to-end

### 12. Pruebas operativas

- [ ] Smoke tests del monorepo levantado
- [ ] Baseline de carga sobre `bot_api`
- [ ] Baseline de carga sobre OCR externo consumiendo cola
- [ ] Validación de timeout y saturación
- [ ] Validación con dependencias reales en ambiente objetivo

## Checklist de retiro del legacy

### 13. Desacople final

- [x] Dejar `legacy_info` en desuso
- [x] Dejar `legacy_contact` en desuso
- [x] Dejar `legacy_ocr` en desuso
- [x] Eliminar fallback automático a legacy cuando el flujo interno ya esté validado para `support`, `billing` y `sales`
- [ ] Congelar las repos legacy solo como referencia histórica

### 14. Cutover

- [ ] Definir estrategia de migración:
  - shadow
  - canary
  - switch directo
- [ ] Definir rollback
- [ ] Preparar smoke test post-deploy
- [ ] Validar tráfico real controlado
- [ ] Confirmar desactivación del stack anterior

## Orden recomendado

1. Cerrar integración OCR externa por cola + callback.
2. Cerrar integraciones reales del monorepo.
3. Completar soporte L1 según alcance de producto.
4. Ejecutar pruebas E2E reales.
5. Retirar fallbacks legacy.
6. Hacer cutover controlado.

## Veredicto práctico

Si el objetivo es una primera salida funcional del monorepo, lo más importante que falta es:

- validación real del OCR externo consumiendo la cola
- validación con integraciones reales
- eliminación progresiva de dependencias legacy

Si el objetivo es un reemplazo total del stack, además falta:

- cerrar el alcance técnico final del soporte L1
- hacer pruebas integradas y cutover formal
