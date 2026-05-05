"""Conversión de audio a texto para mensajes entrantes."""

import asyncio
import base64
import logging
import mimetypes
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from packages.shared.config import get_settings

logger = logging.getLogger("integrations.speech_to_text")


def _attachment_value(attachment, key: str):
    """Devuelve el valor adjunto."""
    if isinstance(attachment, dict):
        return attachment.get(key)
    return getattr(attachment, key, None)


@lru_cache(maxsize=8)
def _load_faster_whisper_model(model_size: str, device: str, compute_type: str):
    """Carga faster whisper model."""
    from faster_whisper import WhisperModel

    return WhisperModel(model_size, device=device, compute_type=compute_type)


class SpeechToTextService:
    """Servicio para las integraciones externas."""
    def __init__(self) -> None:
        """Inicializa el speechtotextservice con la configuracion necesaria."""
        self.settings = get_settings()

    @property
    def enabled(self) -> bool:
        """Indica si la integracion esta habilitada por configuracion."""
        return bool(self.settings.audio_enabled) and self.settings.audio_stt_engine == "faster_whisper"

    def _ffmpeg_bin(self) -> str | None:
        """Devuelve el binario ffmpeg."""
        configured = str(self.settings.audio_ffmpeg_bin or "").strip()
        if configured:
            return configured
        return shutil.which("ffmpeg")

    @staticmethod
    def _audio_suffix(filename: str | None, mime_type: str | None) -> str:
        """Devuelve el sufijo audio."""
        if filename:
            suffix = Path(filename).suffix.strip()
            if suffix:
                return suffix
        ext = mimetypes.guess_extension(str(mime_type or "").split(";")[0].strip().lower())
        return ext or ".bin"

    @staticmethod
    def _attachment_bytes(attachment) -> tuple[bytes | None, str | None, str | None]:
        """Devuelve los bytes adjunto."""
        filename = str(_attachment_value(attachment, "filename") or "").strip() or None
        mime_type = str(_attachment_value(attachment, "mime_type") or "").strip() or None
        base64_data = str(_attachment_value(attachment, "base64_data") or "").strip()
        if not base64_data:
            return None, filename, mime_type
        if base64_data.startswith("data:") and "," in base64_data:
            header, base64_data = base64_data.split(",", 1)
            if not mime_type and ";" in header:
                mime_type = header.split(":", 1)[1].split(";", 1)[0].strip() or mime_type
        try:
            return base64.b64decode(base64_data, validate=False), filename, mime_type
        except Exception:
            logger.exception("speech_to_text_base64_decode_failed filename=%s", filename)
            return None, filename, mime_type

    def _normalize_with_ffmpeg(self, source_path: str, cleanup_paths: list[Path]) -> str:
        """Normaliza ffmpeg with."""
        ffmpeg_bin = self._ffmpeg_bin()
        if not ffmpeg_bin:
            return source_path
        output_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        output_path = Path(output_file.name)
        output_file.close()
        cleanup_paths.append(output_path)
        command = [
            ffmpeg_bin,
            "-y",
            "-i",
            source_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(output_path),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, check=False)
        except OSError:
            logger.exception("speech_to_text_ffmpeg_spawn_failed source=%s", source_path)
            return source_path
        if completed.returncode != 0:
            logger.warning("speech_to_text_ffmpeg_failed source=%s returncode=%s", source_path, completed.returncode)
            return source_path
        return str(output_path)

    def _transcribe_path(self, audio_path: str) -> dict:
        """Transcribe ruta."""
        try:
            model = _load_faster_whisper_model(
                self.settings.audio_stt_model,
                self.settings.audio_stt_device,
                self.settings.audio_stt_compute_type,
            )
        except ImportError:
            return {"status": "unavailable", "reason": "missing_faster_whisper"}

        options = {
            "vad_filter": True,
            "beam_size": 1,
            "condition_on_previous_text": False,
        }
        language = str(self.settings.audio_stt_language or "").strip()
        if language:
            options["language"] = language
        initial_prompt = str(self.settings.audio_stt_prompt or "").strip()
        if initial_prompt:
            options["initial_prompt"] = initial_prompt

        segments, info = model.transcribe(audio_path, **options)
        text = " ".join((segment.text or "").strip() for segment in segments).strip()
        if not text:
            return {
                "status": "empty",
                "engine": "faster_whisper",
                "language": getattr(info, "language", None),
                "duration": getattr(info, "duration", None),
            }
        return {
            "status": "ok",
            "engine": "faster_whisper",
            "text": text,
            "language": getattr(info, "language", None),
            "duration": getattr(info, "duration", None),
        }

    async def transcribe_attachment(self, attachment) -> dict:
        """Convierte un adjunto de audio en texto."""
        if not self.enabled:
            return {"status": "disabled"}

        media_bytes, filename, mime_type = self._attachment_bytes(attachment)
        if not media_bytes:
            return {"status": "missing_audio", "filename": filename, "mime_type": mime_type}

        cleanup_paths: list[Path] = []
        try:
            suffix = self._audio_suffix(filename, mime_type)
            input_file = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            input_path = Path(input_file.name)
            input_file.write(media_bytes)
            input_file.flush()
            input_file.close()
            cleanup_paths.append(input_path)

            audio_path = self._normalize_with_ffmpeg(str(input_path), cleanup_paths)
            result = await asyncio.to_thread(self._transcribe_path, audio_path)
            result["filename"] = filename
            result["mime_type"] = mime_type
            return result
        except Exception as exc:
            logger.exception("speech_to_text_transcription_failed filename=%s mime_type=%s", filename, mime_type)
            return {
                "status": "error",
                "reason": exc.__class__.__name__,
                "filename": filename,
                "mime_type": mime_type,
            }
        finally:
            for path in cleanup_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("speech_to_text_cleanup_failed path=%s", path)
