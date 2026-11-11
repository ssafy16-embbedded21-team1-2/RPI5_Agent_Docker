from typing import Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from rpi5_agent.models import DiagnosticProfile


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    diagnostic_profile: DiagnosticProfile = DiagnosticProfile.PRODUCT

    llm_enabled: bool = False
    llm_base_url: str = "http://127.0.0.1:8080/v1"
    llm_model: str = "local-qwen"
    llm_api_key: SecretStr = SecretStr("not-required")
    llm_timeout_seconds: float = Field(default=120, gt=0, le=300)
    llm_max_tokens: int = Field(default=256, ge=1, le=512)

    external_llm_enabled: bool = False
    external_llm_base_url: str = "https://llm.pamout.com/v1"
    external_llm_model: str = "glm-5.3"
    external_llm_api_key: SecretStr = SecretStr("")
    external_llm_user_agent: str = "rpi5-diagnostic-agent/0.1.0"
    external_llm_timeout_seconds: float = Field(default=120, gt=0, le=300)
    external_llm_max_tokens: int = Field(default=4096, ge=1, le=8192)
    external_llm_response_format: Literal["prompt", "json_object", "json_schema"] = "prompt"
    external_llm_fallback_on_local_failure: bool = True

    @field_validator("llm_base_url", "external_llm_base_url")
    @classmethod
    def validate_base_url(cls, value: str, info: ValidationInfo) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("LLM base URL must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use the API key setting for credentials; omit URL query and fragment")
        if (
            info.field_name == "external_llm_base_url"
            and parsed.scheme != "https"
            and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("External LLM requires HTTPS (HTTP loopback is allowed for tests)")
        return value.rstrip("/")

    @field_validator("llm_model", "external_llm_model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Model ID must not be empty")
        return value.strip()

    @field_validator("external_llm_user_agent")
    @classmethod
    def validate_user_agent(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) > 126 for char in value):
            raise ValueError("External LLM User-Agent must be nonempty printable ASCII")
        return value.strip()

    @model_validator(mode="after")
    def require_external_key(self) -> "Settings":
        if self.external_llm_enabled:
            key = self.external_llm_api_key.get_secret_value()
            if not key.strip() or key == "not-required":
                raise ValueError("EXTERNAL_LLM_API_KEY is required when EXTERNAL_LLM_ENABLED=true")
            if any(ord(char) < 33 or ord(char) > 126 for char in key):
                raise ValueError(
                    "EXTERNAL_LLM_API_KEY must not contain whitespace/control characters"
                )
        return self
