from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_PIPER_DIR = Path.home() / ".local/share/management-assistant/voice"


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    realtime_model: str
    realtime_url: str
    summary_model: str
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from_number: str
    public_base_url: str
    admin_token: str
    db_path: Path
    host: str
    port: int
    https_port: int
    ssl_certfile: str
    ssl_keyfile: str
    validate_twilio_signature: bool
    dialer_enabled: bool
    voice_backend: str
    ollama_base_url: str
    ollama_model: str
    ollama_tools: bool
    gemini_api_key: str
    gemini_model: str
    piper_bin: str
    piper_model: str
    whisper_model: str
    tts_engine: str = "kokoro"
    kokoro_model: str = ""
    kokoro_voices: str = ""
    kokoro_voice: str = "af_heart"
    kokoro_speed: float = 1.0
    # "auto" uses Zadarma SIP when configured, else Twilio.
    phone_provider: str = "auto"
    zadarma_sip_login: str = ""
    zadarma_sip_password: str = ""
    zadarma_sip_server: str = ""
    zadarma_sip_port: int = 5060
    sip_local_port: int = 5062
    sip_public_ip: str = ""
    rtp_port_min: int = 16000
    rtp_port_max: int = 16999
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_graph_version: str = "v23.0"
    whatsapp_auto_answer: bool = True

    @property
    def zadarma_configured(self) -> bool:
        return bool(self.zadarma_sip_login and self.zadarma_sip_password)

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.whatsapp_token and self.whatsapp_phone_number_id and self.whatsapp_verify_token)

    @property
    def phone_line(self) -> str:
        """Which carrier places outbound phone calls: "zadarma", "twilio" or ""."""
        choice = (self.phone_provider or "auto").strip().lower()
        if choice == "zadarma":
            return "zadarma" if self.zadarma_configured else ""
        if choice == "twilio":
            return "twilio" if self.twilio_configured else ""
        if self.zadarma_configured:
            return "zadarma"
        return "twilio" if self.twilio_configured else ""

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def uses_openai_realtime(self) -> bool:
        backend = (self.voice_backend or "auto").strip().lower()
        if backend == "openai":
            return self.openai_configured
        if backend in {"gemini", "ollama", "openai_chat"}:
            return False
        # auto never picks Realtime — Talk/practice uses chat/local unless forced.
        return False

    @property
    def uses_gemini(self) -> bool:
        backend = (self.voice_backend or "auto").strip().lower()
        if backend == "gemini":
            return self.gemini_configured
        if backend in {"openai", "ollama", "openai_chat"}:
            return False
        # auto: Gemini Flash first when a key is set.
        return self.gemini_configured

    @property
    def uses_openai_chat(self) -> bool:
        backend = (self.voice_backend or "auto").strip().lower()
        if backend == "openai_chat":
            return self.openai_configured
        if backend in {"openai", "ollama", "gemini"}:
            return False
        # auto: OpenAI chat if no Gemini key.
        return self.openai_configured and not self.gemini_configured

    @property
    def uses_ollama(self) -> bool:
        backend = (self.voice_backend or "auto").strip().lower()
        if backend == "ollama":
            return True
        if backend in {"openai", "gemini", "openai_chat"}:
            return False
        return not self.openai_configured and not self.gemini_configured

    @property
    def uses_local_voice(self) -> bool:
        """Whisper STT + Piper TTS path (Ollama / Gemini / OpenAI chat for the brain)."""
        return self.uses_ollama or self.uses_gemini or self.uses_openai_chat

    @property
    def voice_available(self) -> bool:
        return self.uses_openai_realtime or self.uses_local_voice

    @property
    def llm_label(self) -> str:
        if self.uses_gemini:
            return f"gemini:{self.gemini_model}"
        if self.uses_openai_chat:
            return f"openai_chat:{self.summary_model}"
        if self.uses_ollama:
            return f"ollama:{self.ollama_model}"
        if self.uses_openai_realtime:
            return f"openai:{self.realtime_model}"
        return "none"

    @property
    def twilio_configured(self) -> bool:
        return bool(
            self.twilio_account_sid
            and self.twilio_auth_token
            and self.twilio_from_number
            and self.public_base_url
        )

    @property
    def public_ws_base(self) -> str:
        base = self.public_base_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):]
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):]
        return base


def load_settings(env_file: str | os.PathLike[str] | None = None) -> Settings:
    if env_file is None:
        env_file = os.environ.get("AGENTVOICE_ENV", ".env")
    load_dotenv(Path(env_file))

    data_dir = Path(os.environ.get("AGENTVOICE_DATA_DIR", Path.home() / ".local/share/agentvoice"))
    db_path = Path(os.environ.get("AGENTVOICE_DB", data_dir / "agentvoice.db"))

    return Settings(
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        realtime_model=os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime"),
        realtime_url=os.environ.get("OPENAI_REALTIME_URL", "wss://api.openai.com/v1/realtime"),
        summary_model=os.environ.get("OPENAI_SUMMARY_MODEL", "gpt-4o-mini"),
        twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
        twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
        twilio_from_number=os.environ.get("TWILIO_FROM_NUMBER", ""),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        admin_token=os.environ.get("ADMIN_TOKEN", ""),
        db_path=db_path,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        # Phone browsers only allow the microphone over HTTPS; 0 disables the HTTPS listener.
        https_port=int(os.environ.get("HTTPS_PORT", "8443") or 0),
        ssl_certfile=os.environ.get("SSL_CERTFILE", ""),
        ssl_keyfile=os.environ.get("SSL_KEYFILE", ""),
        validate_twilio_signature=_bool("TWILIO_VALIDATE_SIGNATURE", True),
        dialer_enabled=_bool("DIALER_ENABLED", True),
        voice_backend=os.environ.get("VOICE_BACKEND", "auto").strip() or "auto",
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        # 1B is much faster on CPU for Talk/practice; override with llama3.2:latest if you prefer.
        ollama_model=os.environ.get("OLLAMA_MODEL", "llama3.2:1b"),
        # Small local models garble tool calls; by default the call flow is rule-driven instead.
        ollama_tools=_bool("OLLAMA_TOOLS", False),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", ""),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        piper_bin=os.environ.get("PIPER_BIN", str(_DEFAULT_PIPER_DIR / "piper" / "piper")),
        piper_model=os.environ.get("PIPER_MODEL", str(_DEFAULT_PIPER_DIR / "en_US-ryan-high.onnx")),
        whisper_model=os.environ.get("WHISPER_MODEL", "tiny.en"),
        tts_engine=os.environ.get("TTS_ENGINE", "kokoro").strip().lower() or "kokoro",
        kokoro_model=os.environ.get("KOKORO_MODEL", str(data_dir / "kokoro" / "kokoro-v1.0.onnx")),
        kokoro_voices=os.environ.get("KOKORO_VOICES", str(data_dir / "kokoro" / "voices-v1.0.bin")),
        kokoro_voice=os.environ.get("KOKORO_VOICE", "af_heart"),
        kokoro_speed=float(os.environ.get("KOKORO_SPEED", "1.0") or 1.0),
        phone_provider=os.environ.get("PHONE_PROVIDER", "auto").strip().lower() or "auto",
        zadarma_sip_login=os.environ.get("ZADARMA_SIP_LOGIN", "").strip(),
        zadarma_sip_password=os.environ.get("ZADARMA_SIP_PASSWORD", ""),
        zadarma_sip_server=os.environ.get("ZADARMA_SIP_SERVER", "").strip(),
        zadarma_sip_port=int(os.environ.get("ZADARMA_SIP_PORT", "5060") or 5060),
        sip_local_port=int(os.environ.get("SIP_LOCAL_PORT", "5062") or 5062),
        sip_public_ip=os.environ.get("SIP_PUBLIC_IP", "").strip(),
        rtp_port_min=int(os.environ.get("RTP_PORT_MIN", "16000") or 16000),
        rtp_port_max=int(os.environ.get("RTP_PORT_MAX", "16999") or 16999),
        whatsapp_token=os.environ.get("WHATSAPP_TOKEN", "").strip(),
        whatsapp_phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip(),
        whatsapp_app_secret=os.environ.get("WHATSAPP_APP_SECRET", "").strip(),
        whatsapp_verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip(),
        whatsapp_graph_version=os.environ.get("WHATSAPP_GRAPH_VERSION", "v23.0").strip() or "v23.0",
        whatsapp_auto_answer=_bool("WHATSAPP_AUTO_ANSWER", True),
    )
