"""
VCPilot — Application Configuration
Loads environment variables and provides a typed settings object.
DB-level config (Minervini rules, risk params) is loaded separately via models.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_env: str = "development"
    app_secret_key: str = "changeme"
    log_level: str = "INFO"

    # Database
    database_url: str = "postgresql://vcpilot:changeme@db:5432/vcpilot"
    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_db: str = "vcpilot"
    postgres_user: str = "vcpilot"
    postgres_password: str = "changeme"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # IBKR
    ibkr_host: str = "ibkr-gateway"
    ibkr_port: int = 4002
    ibkr_client_id: int = 1
    ibkr_account: str = ""
    ibkr_paper_mode: bool = True

    # Data APIs
    fmp_api_key: str = ""

    # WAHA / WhatsApp
    waha_api_url: str = "http://waha:3000"
    waha_api_key: str = "changeme-waha-key"
    waha_session: str = "vcpilot"
    whatsapp_admin_number: str = ""
    whatsapp_admin_jid: str = ""

    # Dashboard
    dashboard_port: int = 8501
    dashboard_password: str = "changeme"

    # Trading defaults (overridden by DB SystemConfig)
    trading_universe: str = "ASX200"
    base_currency: str = "AUD"
    weekly_capital_injection: float = 1000.0

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_paper_trading(self) -> bool:
        return self.ibkr_paper_mode


# Singleton — import this throughout the app
settings = Settings()
