"""Schemas compartidos entre API, agentes e integraciones."""

from typing import Any, Literal
from datetime import datetime, timezone

from pydantic import AliasChoices, BaseModel, Field


class Attachment(BaseModel):
    """Adjunto normalizado que puede viajar con URL o con datos base64."""
    type: str | None = None
    mime_type: str | None = None
    url: str | None = None
    base64_data: str | None = None
    filename: str | None = None


class Location(BaseModel):
    """Ubicación geográfica opcional asociada a un mensaje entrante."""
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None


class InboundMessage(BaseModel):
    """Mensaje normalizado de entrada que viaja desde los canales al orquestador."""
    mensaje: str
    channel: Literal["whatsapp", "messenger", "internal"] = "internal"
    recipient: str = "unknown"
    session_id: str
    cedula: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    location: Location | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MetaWebhookPayload(BaseModel):
    """Payload entrante con el formato esperado por el webhook de Meta."""
    channel: Literal["whatsapp", "messenger"] = "whatsapp"
    recipient: str
    session_id: str
    mensaje: str
    cedula: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)


class RouteDecision(BaseModel):
    """Decisión de ruteo que asigna el mensaje a un agente y explica el motivo."""
    agent: Literal["support", "billing", "sales", "handoff", "clarify"]
    intent: str
    confidence: float
    reason: str
    requires_clarification: bool = False


class AgentResult(BaseModel):
    """Resultado que devuelve un agente después de procesar un mensaje."""
    message: str
    intent: str
    agent: str
    actions: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResponsePlan(BaseModel):
    """Plan de respuesta que resume intención, evidencia y siguiente paso."""
    domain: str | None = None
    conversation_state: str | None = None
    message: str
    reply_goal: str | None = None
    hypothesis: str | None = None
    evidence: list[str] = Field(default_factory=list)
    next_step: str | None = None
    followup_prompt: str | None = None
    should_handoff: bool = False
    handoff_reason: str | None = None


class FlowTurnInterpretation(BaseModel):
    """Interpretacion estructurada de un turno de conversación."""
    action: Literal[
        "answer_current_step",
        "switch_intent",
        "show_catalog",
        "start_capture",
        "start_recommendation",
        "accept_recommended_plan",
        "accept_information",
        "decline_registration",
        "unclear",
    ] = "unclear"
    target_intent: str | None = None
    confidence: float | None = None
    reason: str | None = None
    slot_updates: dict[str, Any] = Field(default_factory=dict)


class SessionState(BaseModel):
    """Estado persistido de una sesión conversacional entre mensajes."""
    session_id: str
    channel: str = "internal"
    recipient: str = "unknown"
    current_intent: str | None = None
    last_agent: str | None = None
    awaiting_field: str | None = None
    cedula: str | None = None
    selected_contract: str | None = None
    human_handoff: bool = False
    history: list[dict[str, str]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_user_message_at: datetime | None = None
    last_assistant_message_at: datetime | None = None


class OutboundMessage(BaseModel):
    """Respuesta final que el orquestador entrega al canal."""
    status: str = "ok"
    message: str
    agent: str
    intent: str
    confidence: float
    requires_clarification: bool = False
    actions: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class OCRJob(BaseModel):
    """Trabajo OCR con el mensaje, los adjuntos y el contrato asociados."""
    job_id: str
    session_id: str
    channel: Literal["whatsapp", "messenger", "internal"] = "internal"
    recipient: str
    cedula: str | None = None
    contract: dict[str, Any] = Field(default_factory=dict)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    message: str = ""
    proof_attempts: int = 0


class OCRCallbackPayload(BaseModel):
    """Payload de callback que devuelve el resultado OCR del trabajo."""
    job: OCRJob
    ocr_result: dict[str, Any] | None = None
    source: str = "ocr_service"


class OutboundDelivery(BaseModel):
    """Instrucción de entrega para enviar un mensaje por canal."""
    session_id: str | None = None
    channel: Literal["whatsapp", "messenger", "internal"] = Field(
        default="internal",
        validation_alias=AliasChoices("channel", "chanel"),
    )
    recipient: str
    message: str
    origen: str = "ia"
    actions: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
