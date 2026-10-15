from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop"
    app_env: Literal["development", "test", "staging", "production"] = "development"
    auth_mode: Literal["disabled", "basic"] = "disabled"
    auth_username: SecretStr | None = None
    auth_password: SecretStr | None = None
    cors_origin: str = "http://localhost:3000"
    enable_docs: bool | None = None
    dev_actor_id: str = "local-developer"

    @field_validator("cors_origin")
    @classmethod
    def cors_origin_is_explicit(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            value == "*"
            or "*" in parsed.netloc
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("CORS_ORIGIN must be one explicit HTTP(S) origin without wildcards")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_auth_configuration(self) -> "Settings":
        if self.auth_mode == "disabled" and self.app_env not in {"development", "test"}:
            raise ValueError("AUTH_MODE=disabled is allowed only in development or test")
        if self.auth_mode == "basic":
            username = self.auth_username.get_secret_value() if self.auth_username else ""
            password = self.auth_password.get_secret_value() if self.auth_password else ""
            if not username or not password:
                raise ValueError("AUTH_USERNAME and AUTH_PASSWORD are required for AUTH_MODE=basic")
        return self

    @property
    def docs_enabled(self) -> bool:
        if self.enable_docs is not None:
            return self.enable_docs
        return self.app_env != "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
