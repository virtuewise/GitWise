from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    app_name: str = "GitWise API"
    environment: str = "development"
    log_level: str = "INFO"
    database_url: str | None = None
    github_webhook_secret: str | None = None
    github_app_id: int | None = None
    github_app_private_key: str | None = None
    # Comma-separated Fernet keys, newest first, for encrypting stored GitHub user tokens.
    token_encryption_keys: SecretStr | None = None
    github_app_client_id: str | None = None
    github_app_client_secret: SecretStr | None = None
    # Public origin of the web app, e.g. https://app.example.com. The OAuth callback is
    # <public_base_url>/auth/github/callback and sign-in always returns to this origin.
    public_base_url: str | None = None

    model_config = SettingsConfigDict(
        env_prefix="GITWISE_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
