"""Tests for app/config.py — Settings properties and DB override logic."""
import pytest
from unittest.mock import patch, MagicMock


def test_settings_has_defaults():
    from app.config import settings
    assert settings.app_env in ("development", "production", "test")
    assert isinstance(settings.dashboard_port, int)
    assert isinstance(settings.redis_url, str)


def test_settings_is_production_false_by_default():
    from app.config import Settings
    import os
    # Create a Settings with app_env forced to 'development' via env var
    orig = os.environ.get("APP_ENV")
    os.environ["APP_ENV"] = "development"
    try:
        s = Settings()
        assert s.is_production is False
    finally:
        if orig is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = orig


def test_settings_is_production_true():
    from app.config import Settings
    import os
    orig = os.environ.get("APP_ENV")
    os.environ["APP_ENV"] = "production"
    try:
        s = Settings()
        assert s.is_production is True
    finally:
        if orig is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = orig


def test_settings_is_paper_trading():
    from app.config import Settings
    s = Settings()
    # ibkr_paper_mode_env defaults to True, and _get_db_config returns None
    with patch.object(s, "_get_db_config", return_value=None):
        assert s.is_paper_trading is True


def test_settings_ibkr_account_db_override():
    from app.config import Settings
    s = Settings()
    with patch.object(s, "_get_db_config", return_value="DU999999"):
        assert s.ibkr_account == "DU999999"


def test_settings_ibkr_account_fallback_to_env():
    from app.config import Settings
    s = Settings()
    s.ibkr_account_env = "DU123456"
    with patch.object(s, "_get_db_config", return_value=None):
        assert s.ibkr_account == "DU123456"


def test_settings_ibkr_paper_mode_db_true():
    from app.config import Settings
    s = Settings()
    with patch.object(s, "_get_db_config", return_value="true"):
        assert s.ibkr_paper_mode is True


def test_settings_ibkr_paper_mode_db_false():
    from app.config import Settings
    s = Settings()
    with patch.object(s, "_get_db_config", return_value="false"):
        assert s.ibkr_paper_mode is False


def test_settings_fmp_api_key_db_override():
    from app.config import Settings
    s = Settings()
    with patch.object(s, "_get_db_config", return_value="fmp_key_123"):
        assert s.fmp_api_key == "fmp_key_123"


def test_settings_working_capital_db_override():
    from app.config import Settings
    s = Settings()
    call_count = {"n": 0}
    def mock_db_config(key):
        if key == "working_capital_aud":
            return "10000"
        return None
    with patch.object(s, "_get_db_config", side_effect=mock_db_config):
        wc = s.working_capital
    assert wc == pytest.approx(10000.0)


def test_settings_working_capital_weekly_injection_fallback():
    from app.config import Settings
    s = Settings()
    def mock_db_config(key):
        if key == "weekly_injection_aud":
            return "2000"
        return None
    with patch.object(s, "_get_db_config", side_effect=mock_db_config):
        wc = s.working_capital
    assert wc == pytest.approx(2000.0)


def test_settings_working_capital_env_fallback():
    from app.config import Settings
    s = Settings()
    s.working_capital_env = 7500.0
    with patch.object(s, "_get_db_config", return_value=None):
        wc = s.working_capital
    assert wc == pytest.approx(7500.0)


def test_settings_telegram_enabled_db():
    from app.config import Settings
    s = Settings()
    with patch.object(s, "_get_db_config", return_value="false"):
        assert s.telegram_enabled is False


def test_settings_telegram_bot_token_db():
    from app.config import Settings
    s = Settings()
    with patch.object(s, "_get_db_config", return_value="bot:TOKEN123"):
        assert s.telegram_bot_token == "bot:TOKEN123"


def test_settings_mock_time_enabled_db():
    from app.config import Settings
    s = Settings()
    with patch.object(s, "_get_db_config", return_value="true"):
        assert s.mock_time_enabled is True


def test_settings_mock_time_enabled_false():
    from app.config import Settings
    s = Settings()
    with patch.object(s, "_get_db_config", return_value=None):
        assert s.mock_time_enabled is False  # Default env is False


def test_settings_mock_current_time_db():
    from app.config import Settings
    s = Settings()
    with patch.object(s, "_get_db_config", return_value="2026-06-10T10:30:00"):
        assert s.mock_current_time == "2026-06-10T10:30:00"


def test_settings_ibkr_simulate_live_db():
    from app.config import Settings
    s = Settings()
    with patch.object(s, "_get_db_config", return_value="true"):
        assert s.ibkr_simulate_live is True


def test_settings_weekly_capital_injection_deprecated():
    from app.config import Settings
    s = Settings()
    with patch.object(s, "_get_db_config", return_value="3000"):
        wc = s.weekly_capital_injection
    assert wc == pytest.approx(3000.0)


def test_get_db_config_returns_none_on_exception():
    from app.config import Settings
    s = Settings()
    with patch("app.config.Settings._get_db_config", side_effect=Exception("DB error")):
        # Direct call on a fresh settings instance that patches SessionLocal
        pass
    # The method catches exceptions and returns None
    with patch("app.database.SessionLocal", side_effect=Exception("no DB")):
        result = s._get_db_config("any_key")
    assert result is None
