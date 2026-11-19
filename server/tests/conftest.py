from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agentvoice.config import Settings
from agentvoice.db import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        openai_api_key="sk-test",
        realtime_model="gpt-realtime",
        realtime_url="ws://127.0.0.1:1/realtime",
        summary_model="gpt-4o-mini",
        twilio_account_sid="AC123",
        twilio_auth_token="authtoken",
        twilio_from_number="+15125550100",
        public_base_url="https://agent.example.com",
        admin_token="secret-token",
        db_path=tmp_path / "test.db",
        host="127.0.0.1",
        port=8080,
        validate_twilio_signature=True,
        dialer_enabled=False,
        voice_backend="openai",
        ollama_base_url="http://127.0.0.1:11434",
        ollama_model="llama3.2:latest",
        piper_bin="",
        piper_model="",
        whisper_model="tiny.en",
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    yield database
    database.close()


@pytest.fixture
def make_settings(settings: Settings):
    def factory(**overrides):
        return replace(settings, **overrides)

    return factory
