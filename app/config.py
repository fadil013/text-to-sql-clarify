"""Central settings, loaded from environment / .env."""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_db: str = "shop"
    postgres_host: str = "localhost"
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
