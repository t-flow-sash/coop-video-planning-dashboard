from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop"
    app_env: str = "development"
    auth_mode: str = "disabled"
    cors_origin: str = "http://localhost:3000"
    dev_actor_id: str = "local-developer"

    @model_validator(mode="after")
    def disabled_auth_is_development_only(self) -> "Settings":
        if self.auth_mode == "disabled" and self.app_env != "development":
            raise ValueError("AUTH_MODE=disabled is allowed only in development")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
