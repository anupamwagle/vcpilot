"""
VCPilot — Application Configuration
Loads environment variables and provides a typed settings object.
DB-level config (Minervini rules, risk params) is loaded separately via models.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional


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
    database_url: str = "postgresql://vcpilot:changeme@database:5432/vcpilot"
    postgres_host: str = "database"
    postgres_port: int = 5432
    postgres_db: str = "vcpilot"
    postgres_user: str = "vcpilot"
    postgres_password: str = "changeme"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # IBKR
    ibkr_host: str = "ibkr"
    ibkr_port: int = 4002
    ibkr_client_id: int = 1
    ibkr_account_env: str = Field(default="", validation_alias="ibkr_account")
    ibkr_username_env: str = Field(default="", validation_alias="ibkr_username")
    ibkr_password_env: str = Field(default="", validation_alias="ibkr_password")
    ibkr_paper_mode_env: bool = Field(default=True, validation_alias="ibkr_paper_mode")

    # Data APIs
    fmp_api_key_env: str = Field(default="", validation_alias="fmp_api_key")

    # WAHA / WhatsApp
    waha_api_url: str = "http://whatsapp:3000"
    waha_api_key: str = "changeme-waha-key"
    waha_session: str = "default"         # WAHA Core only supports 'default'
    waha_hook_url: str = ""               # e.g. http://dashboard:8501/webhook/whatsapp
    whatsapp_enabled_env: bool = Field(default=True, validation_alias="whatsapp_enabled")
    whatsapp_admin_number_env: str = Field(default="", validation_alias="whatsapp_admin_number")
    whatsapp_admin_jid_env: str = Field(default="", validation_alias="whatsapp_admin_jid")

    # Dashboard
    dashboard_port: int = 8501
    dashboard_password: str = "changeme"

    # Super Admin
    superadmin_email: str = "superadmin@astradigital.com.au"
    superadmin_password: str = "superadmin-pass"

    # SMTP / Email
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_from_email: str = ""
    smtp_from_name: str = "VCPilot"


    # Trading defaults (overridden by DB SystemConfig)
    trading_universe: str = "ASX200"
    base_currency: str = "AUD"
    weekly_capital_injection_env: float = Field(default=1000.0, validation_alias="weekly_capital_injection")

    def _get_db_config(self, key: str) -> Optional[str]:
        """Fetch config value directly from database to avoid circular import issues."""
        try:
            from app.database import SessionLocal
            from sqlalchemy import text
            db = SessionLocal()
            try:
                res = db.execute(
                    text("SELECT value FROM system_configs WHERE key = :key"),
                    {"key": key}
                ).fetchone()
                if res and res[0] is not None:
                    return res[0]
            finally:
                db.close()
        except Exception:
            pass
        return None

    @property
    def ibkr_account(self) -> str:
        val = self._get_db_config("ibkr_account")
        return val if val is not None else self.ibkr_account_env

    @property
    def ibkr_username(self) -> str:
        val = self._get_db_config("ibkr_username")
        return val if val is not None else self.ibkr_username_env

    @property
    def ibkr_password(self) -> str:
        val = self._get_db_config("ibkr_password")
        return val if val is not None else self.ibkr_password_env

    @property
    def ibkr_paper_mode(self) -> bool:
        val = self._get_db_config("ibkr_paper_mode")
        if val is not None:
            return val.lower() in ("true", "1", "yes")
        return self.ibkr_paper_mode_env

    @property
    def fmp_api_key(self) -> str:
        val = self._get_db_config("fmp_api_key")
        return val if val is not None else self.fmp_api_key_env

    @property
    def whatsapp_enabled(self) -> bool:
        val = self._get_db_config("whatsapp_enabled")
        if val is not None:
            return val.lower() in ("true", "1", "yes")
        return self.whatsapp_enabled_env

    @property
    def whatsapp_admin_number(self) -> str:
        val = self._get_db_config("whatsapp_admin_number")
        return val if val is not None else self.whatsapp_admin_number_env

    @property
    def weekly_capital_injection(self) -> float:
        val = self._get_db_config("weekly_injection_aud")
        if val is not None:
            try:
                return float(val)
            except ValueError:
                pass
        return self.weekly_capital_injection_env

    @property
    def admin_jid(self) -> str:
        """Derive JID from phone number if not explicitly set."""
        db_num = self._get_db_config("whatsapp_admin_number")
        if db_num:
            num = db_num.lstrip("+").replace(" ", "")
            return f"{num}@c.us"

        if self.whatsapp_admin_jid_env:
            return self.whatsapp_admin_jid_env
        if self.whatsapp_admin_number_env:
            num = self.whatsapp_admin_number_env.lstrip("+").replace(" ", "")
            return f"{num}@c.us"
        return ""

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_paper_trading(self) -> bool:
        return self.ibkr_paper_mode


# Singleton — import this throughout the app
settings = Settings()
