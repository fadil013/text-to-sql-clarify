"""Central settings, loaded from environment / .env."""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"

    postgres_db: str = "shop"
    postgres_host: str = "127.0.0.1"  # not "localhost": avoids a ~20s IPv6 (::1) timeout on Windows
    # before falling back to IPv4, since docker-compose binds the port to 127.0.0.1 only.
    postgres_port: int = 5432
    postgres_admin_user: str = "postgres"
    postgres_admin_password: str = Field(default="")
    postgres_readonly_user: str = "app_readonly"
    postgres_readonly_password: str = Field(default="")

    statement_timeout_ms: int = 5000
    max_rows: int = 1000

    def _dsn(self, user: str, password: str) -> str:
        return (
            f"host={self.postgres_host} port={self.postgres_port} dbname={self.postgres_db} "
            f"user={user} password={password}"
        )

    @property
    def admin_dsn(self) -> str:
        return self._dsn(self.postgres_admin_user, self.postgres_admin_password)

    @property
    def readonly_dsn(self) -> str:
        return self._dsn(self.postgres_readonly_user, self.postgres_readonly_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
