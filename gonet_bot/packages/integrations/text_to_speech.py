"""Síntesis de voz para respuestas audibles."""

import asyncio
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from packages.shared.config import get_settings

logger = logging.getLogger("integrations.text_to_speech")


class TextToSpeechService:
    """Servicio para las integraciones externas."""
    def __init__(self) -> None:
        """Inicializa el texttospeechservice con la configuracion necesaria."""
        self.settings = get_settings()

    @property
    def enabled(self) -> bool:
        """Indica si la integracion esta habilitada por configuracion."""
        return bool(self.settings.audio_enabled) and self.settings.audio_tts_engine in {"edge_tts", "piper"}

    def _ffmpeg_bin(self) -> str | None:
        """Devuelve el binario ffmpeg."""
        configured = str(self.settings.audio_ffmpeg_bin or "").strip()
        if configured:
            return configured
        return shutil.which("ffmpeg")

    def _resolve_voice(self, metadata: dict | None = None) -> str:
        """Resuelve voice."""
        assistant_profile = {}
        if isinstance(metadata, dict):
            candidate = metadata.get("assistant_profile")
            if isinstance(candidate, dict):
                assistant_profile = candidate
        explicit_voice = str(assistant_profile.get("voice") or "").strip()
        if explicit_voice:
            return explicit_voice
        voice_gender = str(assistant_profile.get("voice_gender") or "").strip().lower()
        if voice_gender == "female":
            return str(self.settings.audio_tts_voice_female or self.settings.audio_tts_voice).strip()
        if voice_gender == "male":
            return str(self.settings.audio_tts_voice_male or self.settings.audio_tts_voice).strip()
        return str(self.settings.audio_tts_voice).strip()

    def _finalize_audio_file(self, source_path: str, *, engine: str, mime_type: str, filename: str) -> dict:
        """Devuelve el file finalize audio."""
        source = Path(source_path)
        cleanup_paths = [source]
        ffmpeg_bin = self._ffmpeg_bin()
        try:
            if ffmpeg_bin:
                output_file = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
                output_path = Path(output_file.name)
                output_file.close()
                cleanup_paths.append(output_path)
                command = [
                    ffmpeg_bin,
                    "-y",
                    "-i",
                    str(source),
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "48k",
                    str(output_path),
                ]
                try:
                    completed = subprocess.run(command, capture_output=True, check=False)
                    if completed.returncode == 0:
                        return {
                            "status": "ok",
                            "engine": engine,
                            "media_bytes": output_path.read_bytes(),
                            "mime_type": "audio/ogg",
                            "filename": f"{source.stem}.ogg",
                        }
                    logger.warning(
                        "text_to_speech_ffmpeg_failed engine=%s source=%s returncode=%s",
                        engine,
                        source,
                        completed.returncode,
                    )
                except OSError:
                    logger.exception("text_to_speech_ffmpeg_spawn_failed engine=%s source=%s", engine, source)

            media_bytes = source.read_bytes()
        except OSError as exc:
            return {"status": "error", "engine": engine, "reason": exc.__class__.__name__}
        finally:
            for path in cleanup_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("text_to_speech_cleanup_failed path=%s", path)
        return {
            "status": "ok",
            "engine": engine,
            "media_bytes": media_bytes,
            "mime_type": mime_type,
            "filename": filename,
        }

    async def _synthesize_edge_tts(self, text: str, *, voice: str) -> dict:
        """Sintetiza edge tts."""
        try:
            import edge_tts
        except ImportError:
            return {"status": "unavailable", "engine": "edge_tts", "reason": "missing_edge_tts", "voice": voice}

        output_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        output_path = output_file.name
        output_file.close()
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(output_path)
            result = self._finalize_audio_file(
                output_path,
                engine="edge_tts",
                mime_type="audio/mpeg",
                filename="reply.mp3",
            )
            result["voice"] = voice
            return result
        except Exception as exc:
            Path(output_path).unlink(missing_ok=True)
            logger.exception("text_to_speech_edge_tts_failed voice=%s", voice)
            return {"status": "error", "engine": "edge_tts", "reason": exc.__class__.__name__, "voice": voice}

    def _synthesize_piper(self, text: str) -> dict:
        """Sintetiza piper."""
        piper_bin = str(self.settings.audio_tts_piper_bin or "").strip() or shutil.which("piper")
        model_path = str(self.settings.audio_tts_piper_model or "").strip()
        if not piper_bin or not model_path:
            return {"status": "unavailable", "engine": "piper", "reason": "missing_piper_binary_or_model"}

        output_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        output_path = output_file.name
        output_file.close()
        command = [
            piper_bin,
            "--model",
            model_path,
            "--output_file",
            output_path,
        ]
        try:
            completed = subprocess.run(command, input=text.encode("utf-8"), capture_output=True, check=False)
        except OSError as exc:
            Path(output_path).unlink(missing_ok=True)
            logger.exception("text_to_speech_piper_spawn_failed")
            return {"status": "error", "engine": "piper", "reason": exc.__class__.__name__}
        if completed.returncode != 0:
            Path(output_path).unlink(missing_ok=True)
            return {"status": "error", "engine": "piper", "reason": "piper_failed"}
        return self._finalize_audio_file(
            output_path,
            engine="piper",
            mime_type="audio/wav",
            filename="reply.wav",
        )

    async def synthesize(self, text: str, *, metadata: dict | None = None) -> dict:
        """Convierte texto en audio."""
        if not self.enabled:
            return {"status": "disabled"}
        clean_text = " ".join(str(text or "").split()).strip()
        if not clean_text:
            return {"status": "empty"}
        voice = self._resolve_voice(metadata)
        if self.settings.audio_tts_engine == "edge_tts":
            return await self._synthesize_edge_tts(clean_text, voice=voice)
        return await asyncio.to_thread(self._synthesize_piper, clean_text)
