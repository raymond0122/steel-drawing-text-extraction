"""Every setting the app reads, in one place.

Values come from environment variables first, then from a .env file in the
working directory (copy .env.example to .env). Only live mode needs the Azure
and OpenAI settings; the auth API needs JWT_SECRET_KEY.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(".env")  # relative to the working directory, like the commands in the README
DEFAULT_OPENAI_MODEL = "gpt-4o"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


class Settings(BaseSettings):
    # Spaces around a value are removed. Empty values in .env are ignored, so they
    # cannot blank out a variable set in the shell.
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8", env_ignore_empty=True, str_strip_whitespace=True, extra="ignore"
    )

    # Azure AI Document Intelligence (OCR)
    azure_docint_endpoint: str = ""
    azure_docint_key: str = ""

    # OpenAI (LLM label filter)
    openai_api_key: str = ""
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_base_url: str = DEFAULT_OPENAI_BASE_URL

    # Auth API. Without JWT_SECRET_KEY a random key is used, so tokens stop working on restart.
    jwt_secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    auth_db_path: Path = Path("auth.db")

    @property
    def jwt_secret_is_random(self) -> bool:
        return "jwt_secret_key" not in self.model_fields_set


def load_settings(env_file: Path | None = ENV_FILE) -> Settings:
    """Read the settings now. Environment variables win over the .env file; None reads no file."""
    return Settings(_env_file=env_file)


def is_filled(value: str) -> bool:
    """False for an empty value and for a "<your-resource>" placeholder left over from .env.example."""
    return bool(value) and "<" not in value
