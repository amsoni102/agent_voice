from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    validate_twilio_signature: bool
    dialer_enabled: bool

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)

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
        validate_twilio_signature=_bool("TWILIO_VALIDATE_SIGNATURE", True),
        dialer_enabled=_bool("DIALER_ENABLED", True),
    )
