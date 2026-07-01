"""Tests for app/notifications/__init__.py (notifier factory)."""
import pytest
from unittest.mock import patch, MagicMock


# --- get_notifier factory ---

def test_get_notifier_returns_object():
    from app.notifications import get_notifier
    notifier = get_notifier(organization_id=None)
    assert notifier is not None


def test_get_notifier_has_send_method():
    from app.notifications import get_notifier
    notifier = get_notifier(organization_id=None)
    assert callable(getattr(notifier, "send", None))


def test_get_notifier_returns_telegram_notifier():
    from app.notifications import get_notifier
    from app.notifications.telegram import TelegramNotifier
    notifier = get_notifier(organization_id=None)
    assert isinstance(notifier, TelegramNotifier)
