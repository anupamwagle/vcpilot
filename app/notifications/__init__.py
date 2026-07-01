"""Notifications package."""
from __future__ import annotations
from app.notifications.base import BaseNotifier

def get_notifier(organization_id: int | None = None) -> BaseNotifier:
    """Factory to retrieve the active notifier for an organization. Telegram is the only channel."""
    from app.notifications.telegram import TelegramNotifier

    return TelegramNotifier(organization_id=organization_id)
