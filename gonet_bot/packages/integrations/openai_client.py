"""Adaptador ligero para llamadas al SDK de OpenAI."""

import json
import re
from datetime import datetime, timedelta, timezone

from openai import AsyncOpenAI

from packages.shared.config import get_settings
from packages.shared.schemas import Attachment


class OpenAIClient:
    """Cliente de OpenAI para clasificar entradas, reescribir texto y extraer JSON."""
    _runtime_outage_until: datetime | None = None
    _runtime_last_error: str | None = None

    def __init__(self) -> None:
        """Inicializa el cliente de openai para clasificar entradas, reescribir texto y extraer json con la configuracion necesaria."""
        self.settings = get_settings()
        self._client: AsyncOpenAI | None = None

    def enabled(self) -> bool:
        """Indica si la integracion esta habilitada por configuracion."""
        return bool(str(self.settings.openai_api_key or "").strip())

    @classmethod
    def runtime_unavailable(cls) -> bool:
        """Indica si el runtime esta temporalmente no disponible."""
        if cls._runtime_outage_until is None:
            return False
        if datetime.now(timezone.utc) >= cls._runtime_outage_until:
            cls.clear_runtime_failure()
            return False
        return True

    @classmethod
    def runtime_failure_detail(cls) -> str | None:
        """Devuelve el detalle del ultimo fallo de runtime registrado."""
        if not cls.runtime_unavailable():
            return None
        return cls._runtime_last_error

    @classmethod
    def clear_runtime_failure(cls) -> None:
        """Limpia el estado de fallo temporal de OpenAI."""
        cls._runtime_outage_until = None
        cls._runtime_last_error = None

    def _mark_runtime_failure(self, exc: Exception) -> None:
        """Marca runtime failure con la información confirmada."""
        cooldown = max(int(getattr(self.settings, "openai_runtime_outage_seconds", 180) or 180), 1)
        type(self)._runtime_outage_until = datetime.now(timezone.utc) + timedelta(seconds=cooldown)
        type(self)._runtime_last_error = f"{exc.__class__.__name__}: {exc}"

    def _clear_runtime_failure(self) -> None:
        """Limpia el estado de fallo temporal de OpenAI."""
        type(self).clear_runtime_failure()

    def _sdk(self) -> AsyncOpenAI:
        """Crea y reutiliza el cliente oficial de OpenAI."""
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.settings.openai_api_key,
                timeout=12.0,
                max_retries=1,
            )
        return self._client

    async def classify(self, text: str) -> dict:
        """Devuelve un resultado minimo que confirma si OpenAI está habilitado."""
        return {"enabled": self.enabled(), "text": text}

    @staticmethod
    def _extract_json_object(text: str) -> dict | None:
        """Extrae json object."""
        raw = str(text or "").strip()
        if not raw:
            return None
        candidates = [raw]
        fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
        if fenced and fenced != raw:
            candidates.append(fenced)
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = raw[start : end + 1]
            if snippet not in candidates:
                candidates.append(snippet)
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    async def rewrite_text(
        self,
        *,
        instructions: str,
        payload: dict,
        max_output_tokens: int = 220,
        temperature: float = 0.3,
    ) -> dict:
        """Reescribe el payload con OpenAI siguiendo las instrucciones dadas."""
        if not self.enabled():
            return {"status": "disabled"}
        try:
            response = await self._sdk().responses.create(
                model=self.settings.openai_model,
                instructions=instructions,
                input=json.dumps(payload, ensure_ascii=False),
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                store=False,
            )
        except Exception as exc:
            self._mark_runtime_failure(exc)
            return {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"}
        self._clear_runtime_failure()
        text = str(getattr(response, "output_text", "") or "").strip()
        if not text:
            return {"status": "empty", "response_id": getattr(response, "id", None)}
        return {
            "status": "ok",
            "text": text,
            "response_id": getattr(response, "id", None),
            "model": self.settings.openai_model,
        }

    async def extract_json(
        self,
        *,
        instructions: str,
        payload: dict,
        max_output_tokens: int = 220,
        temperature: float = 0.0,
    ) -> dict:
        """Pide a OpenAI una respuesta JSON y la valida al volver."""
        if not self.enabled():
            return {"status": "disabled"}
        try:
            response = await self._sdk().responses.create(
                model=self.settings.openai_model,
                instructions=instructions,
                input=json.dumps(payload, ensure_ascii=False),
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                store=False,
            )
        except Exception as exc:
            self._mark_runtime_failure(exc)
            return {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"}
        self._clear_runtime_failure()
        text = str(getattr(response, "output_text", "") or "").strip()
        if not text:
            return {"status": "empty", "response_id": getattr(response, "id", None)}
        parsed = self._extract_json_object(text)
        if parsed is None:
            return {
                "status": "invalid_json",
                "text": text,
                "response_id": getattr(response, "id", None),
            }
        return {
            "status": "ok",
            "result": parsed,
            "response_id": getattr(response, "id", None),
            "model": self.settings.openai_model,
        }

    async def classify_conversation(
        self,
        *,
        instructions: str,
        payload: dict,
        max_output_tokens: int = 220,
        temperature: float = 0.0,
    ) -> dict:
        """Clasifica la conversación usando la ruta de extracción JSON."""
        return await self.extract_json(
            instructions=instructions,
            payload=payload,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )

    @staticmethod
    def _input_image_content(attachment: Attachment | dict | None) -> dict | None:
        """Convierte un adjunto en contenido de imagen compatible con OpenAI."""
        if attachment is None:
            return None
        if isinstance(attachment, dict):
            mime_type = str(attachment.get("mime_type") or "").strip() or "image/jpeg"
            base64_data = str(attachment.get("base64_data") or "").strip()
            url = str(attachment.get("url") or "").strip()
        else:
            mime_type = str(attachment.mime_type or "").strip() or "image/jpeg"
            base64_data = str(attachment.base64_data or "").strip()
            url = str(attachment.url or "").strip()
        if mime_type and not mime_type.lower().startswith("image/"):
            return None
        if base64_data:
            if base64_data.startswith("data:"):
                image_url = base64_data
            else:
                image_url = f"data:{mime_type};base64,{base64_data}"
            return {"type": "input_image", "image_url": image_url}
        if url:
            return {"type": "input_image", "image_url": url}
        return None

    async def classify_attachment_intent(
        self,
        *,
        instructions: str,
        payload: dict,
        attachment: Attachment | dict | None,
        max_output_tokens: int = 120,
        temperature: float = 0.0,
    ) -> dict:
        """Clasifica la intención del adjunto usando OpenAI y una imagen de entrada."""
        if not self.enabled():
            return {"status": "disabled"}
        image_content = self._input_image_content(attachment)
        if image_content is None:
            return {"status": "unsupported_attachment"}
        content = [
            {"type": "input_text", "text": json.dumps(payload, ensure_ascii=False)},
            image_content,
        ]
        try:
            response = await self._sdk().responses.create(
                model=self.settings.openai_model,
                instructions=instructions,
                input=[{"role": "user", "content": content}],
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                store=False,
            )
        except Exception as exc:
            self._mark_runtime_failure(exc)
            return {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"}
        self._clear_runtime_failure()
        text = str(getattr(response, "output_text", "") or "").strip()
        if not text:
            return {"status": "empty", "response_id": getattr(response, "id", None)}
        parsed = self._extract_json_object(text)
        if parsed is None:
            return {
                "status": "invalid_json",
                "text": text,
                "response_id": getattr(response, "id", None),
            }
        return {
            "status": "ok",
            "result": parsed,
            "response_id": getattr(response, "id", None),
            "model": self.settings.openai_model,
        }
