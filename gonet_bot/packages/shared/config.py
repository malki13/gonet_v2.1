"""Configuracion central cargada desde `.env` y variables de entorno."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Modelo central de configuracion de la plataforma."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "gonet-platform-bot-api"
    app_env: str = Field(default="local", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    port: int = Field(default=8010, alias="PORT")
    mock_mode: bool = Field(default=False, alias="MOCK_MODE")
    public_base_url: str | None = Field(default=None, alias="PUBLIC_BASE_URL")
    assistant_name: str = Field(default="Daniela", alias="ASSISTANT_NAME")
    assistant_names: str = Field(
        default="Andrea,Andres,Camila,Daniela,Diego,Jorge,Kevin,Luis,Mateo,Paola,Sofia,Valeria",
        alias="ASSISTANT_NAMES",
    )
    max_inbound_chars: int = Field(default=800, alias="MAX_INBOUND_CHARS")
    max_inbound_words: int = Field(default=140, alias="MAX_INBOUND_WORDS")
    audio_enabled: bool = Field(default=False, alias="AUDIO_ENABLED")
    audio_reply_mode: str = Field(default="off", alias="AUDIO_REPLY_MODE")
    audio_stt_engine: str = Field(default="faster_whisper", alias="AUDIO_STT_ENGINE")
    audio_stt_model: str = Field(default="small", alias="AUDIO_STT_MODEL")
    audio_stt_device: str = Field(default="cpu", alias="AUDIO_STT_DEVICE")
    audio_stt_compute_type: str = Field(default="int8", alias="AUDIO_STT_COMPUTE_TYPE")
    audio_stt_language: str = Field(default="es", alias="AUDIO_STT_LANGUAGE")
    audio_stt_prompt: str | None = Field(default=None, alias="AUDIO_STT_PROMPT")
    audio_tts_engine: str = Field(default="edge_tts", alias="AUDIO_TTS_ENGINE")
    audio_tts_voice: str = Field(default="es-EC-LuisNeural", alias="AUDIO_TTS_VOICE")
    audio_tts_voice_female: str | None = Field(default="es-EC-AndreaNeural", alias="AUDIO_TTS_VOICE_FEMALE")
    audio_tts_voice_male: str | None = Field(default="es-EC-LuisNeural", alias="AUDIO_TTS_VOICE_MALE")
    audio_tts_piper_bin: str | None = Field(default=None, alias="AUDIO_TTS_PIPER_BIN")
    audio_tts_piper_model: str | None = Field(default=None, alias="AUDIO_TTS_PIPER_MODEL")
    audio_ffmpeg_bin: str | None = Field(default=None, alias="AUDIO_FFMPEG_BIN")
    audio_max_seconds: int = Field(default=90, alias="AUDIO_MAX_SECONDS")

    redis_url: str | None = Field(default=None, alias="REDIS_URL")
    memory_ttl_seconds: int = Field(default=3600, alias="MEMORY_TTL_SECONDS")
    url_odoo_chat: str | None = Field(default=None, alias="URL_ODOO_CHAT")
    odoo_public_token: str = Field(default="mi_public_token", alias="ODOO_PUBLIC_TOKEN")
    odoo_client_email: str = Field(default="prueba3@gonet.ec", alias="ODOO_CLIENT_EMAIL")
    info_origen: str = Field(default="iainfo", alias="INFO_ORIGEN")
    verify_token: str | None = Field(default=None, alias="VERIFY_TOKEN")
    meta_app_secret: str | None = Field(default=None, alias="META_APP_SECRET")
    token_whatsapp: str | None = Field(default=None, alias="TOKEN_WHATSAPP")
    page_access_token: str | None = Field(default=None, alias="PAGE_ACCESS_TOKEN")
    url_wpp: str | None = Field(default=None, alias="URL_WPP")
    url_msg: str | None = Field(default=None, alias="URL_MSG")
    whatsapp_media_token: str | None = Field(default=None, alias="WHATSAPP_MEDIA_TOKEN")
    whatsapp_graph_version: str = Field(default="v22.0", alias="WHATSAPP_GRAPH_VERSION")
    dry_run_externals: bool = Field(default=False, alias="DRY_RUN_EXTERNALS")
    enable_inactivity_scheduler: bool = Field(default=True, alias="ENABLE_INACTIVITY_SCHEDULER")
    time_inactive_chat: int = Field(default=180, alias="TIME_INACTIVE_CHAT")
    time_inactive_chat_ia: int = Field(default=10, alias="TIME_INACTIVE_CHAT_IA")
    update_inactive_chat: float = Field(default=1.5, alias="UPDATE_INACTIVE_CHAT")
    send_inactive_chat: float = Field(default=1.0, alias="SEND_INACTIVE_CHAT")

    promotions_url: str | None = Field(default=None, alias="PROMOTIONS_URL")
    coverage_lookup_url: str | None = Field(default=None, alias="COVERAGE_LOOKUP_URL")
    pg_dsn: str | None = Field(default=None, alias="PG_DSN")
    contact_pg_dsn: str | None = Field(default=None, alias="CONTACT_PG_DSN")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    otp_pg_dsn: str | None = Field(default=None, alias="OTP_PG_DSN")
    agencies_pg_dsn: str | None = Field(default=None, alias="AGENCIES_PG_DSN")
    geocoder_url: str = Field(default="https://nominatim.openstreetmap.org/reverse", alias="GEOCODER_URL")
    geocoder_user_agent: str = Field(default="gonet-platform/0.1", alias="GEOCODER_USER_AGENT")
    contact_center_lookup_url: str | None = Field(default=None, alias="CONTACT_CENTER_LOOKUP_URL")
    franchise_xmlrpc_host_map: str | None = Field(default=None, alias="FRANCHISE_XMLRPC_HOST_MAP")

    odoo_url: str | None = Field(default=None, alias="ODOO_URL")
    odoo_db: str | None = Field(default=None, alias="ODOO_DB")
    odoo_username: str | None = Field(default=None, alias="ODOO_USERNAME")
    odoo_password: str | None = Field(default=None, alias="ODOO_PASSWORD")
    odoo_lead_model: str = Field(default="crm.lead", alias="ODOO_LEAD_MODEL")
    odoo_jsonrpc_url: str | None = Field(default=None, alias="ODOO_JSONRPC_URL")
    odoo_jsonrpc_db: str | None = Field(default=None, alias="ODOO_JSONRPC_DB")
    odoo_jsonrpc_uid: int | None = Field(default=None, alias="ODOO_JSONRPC_UID")
    odoo_jsonrpc_username: str | None = Field(default=None, alias="ODOO_JSONRPC_USERNAME")
    odoo_jsonrpc_password: str | None = Field(default=None, alias="ODOO_JSONRPC_PASSWORD")
    odoo_jsonrpc_web_url: str | None = Field(default=None, alias="ODOO_JSONRPC_WEB_URL")

    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str | None = Field(default=None, alias="SMTP_USER")
    smtp_pass: str | None = Field(default=None, alias="SMTP_PASS")
    smtp_from: str = Field(default="GoNet <no-reply@gonet.ec>", alias="SMTP_FROM")

    otp_max_attempts: int = Field(default=3, alias="OTP_MAX_ATTEMPTS")
    otp_code_len: int = Field(default=6, alias="OTP_CODE_LEN")
    otp_resend_cooldown_seconds: int = Field(default=120, alias="OTP_RESEND_COOLDOWN_SECONDS")
    otp_ttl_seconds: int = Field(default=300, alias="OTP_TTL_SECONDS")
    otp_lockout_seconds: int = Field(default=900, alias="OTP_LOCKOUT_SECONDS")
    otp_hmac_secret: str = Field(default="", alias="OTP_HMAC_SECRET")
    otp_store_plaintext: bool = Field(default=False, alias="OTP_STORE_PLAINTEXT")

    smart_telcom_base_url: str | None = Field(default=None, alias="SMART_TELCOM_BASE_URL")
    smart_telcom_auth_url: str | None = Field(default=None, alias="SMART_TELCOM_AUTH_URL")
    smart_telcom_email: str | None = Field(default=None, alias="SMART_TELCOM_EMAIL")
    smart_telcom_password: str | None = Field(default=None, alias="SMART_TELCOM_PASSWORD")
    smart_telcom_login_token: str | None = Field(default=None, alias="SMART_TELCOM_LOGIN_TOKEN")
    smart_telcom_token: str | None = Field(default=None, alias="SMART_TELCOM_TOKEN")
    smart_telcom_timeout_seconds: float = Field(default=180.0, alias="SMART_TELCOM_TIMEOUT_SECONDS")
    onu_base_url: str | None = Field(default=None, alias="ONU_BASE_URL")
    temp_net_exclude_ids: str = Field(default="1,5", alias="TEMP_NET_EXCLUDE_IDS")

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    openai_human_handoff_on_runtime_failure: bool = Field(default=True, alias="OPENAI_HUMAN_HANDOFF_ON_RUNTIME_FAILURE")
    openai_runtime_outage_seconds: int = Field(default=180, alias="OPENAI_RUNTIME_OUTAGE_SECONDS")
    conversation_classifier_enabled: bool = Field(default=True, alias="CONVERSATION_CLASSIFIER_ENABLED")
    conversation_classifier_mode: str = Field(default="auto", alias="CONVERSATION_CLASSIFIER_MODE")
    flow_turn_interpreter_enabled: bool = Field(default=True, alias="FLOW_TURN_INTERPRETER_ENABLED")
    flow_turn_interpreter_mode: str = Field(default="auto", alias="FLOW_TURN_INTERPRETER_MODE")
    sales_recommendation_extractor_enabled: bool = Field(default=True, alias="SALES_RECOMMENDATION_EXTRACTOR_ENABLED")
    sales_recommendation_extractor_mode: str = Field(default="auto", alias="SALES_RECOMMENDATION_EXTRACTOR_MODE")
    conversational_renderer_enabled: bool = Field(default=True, alias="CONVERSATIONAL_RENDERER_ENABLED")
    conversational_renderer_mode: str = Field(default="auto", alias="CONVERSATIONAL_RENDERER_MODE")
    conversational_renderer_max_chars: int = Field(default=550, alias="CONVERSATIONAL_RENDERER_MAX_CHARS")
    inbound_coalescing_enabled: bool = Field(default=True, alias="INBOUND_COALESCING_ENABLED")
    inbound_coalescing_window_seconds: float = Field(default=6.0, alias="INBOUND_COALESCING_WINDOW_SECONDS")
    inbound_coalescing_max_messages: int = Field(default=8, alias="INBOUND_COALESCING_MAX_MESSAGES")
    ocr_service_url: str | None = Field(default=None, alias="OCR_SERVICE_URL")
    ocr_service_timeout_seconds: float = Field(default=60.0, alias="OCR_SERVICE_TIMEOUT_SECONDS")
    franchise_aes_key: str | None = Field(default=None, alias="FRANCHISE_AES_KEY")
    franchise_aes_iv_base64: str | None = Field(default=None, alias="FRANCHISE_AES_IV_BASE64")

    ocr_async_enabled: bool = Field(default=False, alias="OCR_ASYNC_ENABLED")
    ocr_queue_name: str = Field(default="ocr:jobs", alias="OCR_QUEUE_NAME")
    ocr_queue_block_seconds: int = Field(default=5, alias="OCR_QUEUE_BLOCK_SECONDS")
    ocr_worker_poll_seconds: float = Field(default=2.0, alias="OCR_WORKER_POLL_SECONDS")
    ocr_callback_secret: str | None = Field(default=None, alias="OCR_CALLBACK_SECRET")
    ocr_callback_lock_ttl_seconds: int = Field(default=300, alias="OCR_CALLBACK_LOCK_TTL_SECONDS")
    ocr_callback_result_ttl_seconds: int = Field(default=604800, alias="OCR_CALLBACK_RESULT_TTL_SECONDS")
    bot_api_internal_secret: str | None = Field(default=None, alias="BOT_API_INTERNAL_SECRET")

    @property
    def contact_registry_dsn(self) -> str | None:
        """Devuelve el dsn contact registry."""
        return self.contact_pg_dsn or self.database_url

    @property
    def allow_insecure_local_bypass(self) -> bool:
        """Decide si conviene permitir insecure local bypass."""
        return self.mock_mode or str(self.app_env or "").strip().lower() == "local"

    def validate_required_runtime_secrets(self) -> None:
        """Valida required runtime secrets."""
        if self.allow_insecure_local_bypass:
            return

        missing: list[str] = []
        if not str(self.bot_api_internal_secret or "").strip():
            missing.append("BOT_API_INTERNAL_SECRET")
        if not str(self.meta_app_secret or "").strip():
            missing.append("META_APP_SECRET")
        if missing:
            raise RuntimeError(f"missing_required_runtime_secrets:{','.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Devuelve settings."""
    return Settings()
