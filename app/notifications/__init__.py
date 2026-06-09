"""Notifications package."""
from __future__ import annotations
from app.notifications.base import BaseNotifier

def get_notifier(organization_id: int | None = None) -> BaseNotifier:
    """Factory to retrieve the active notifier (e.g. WhatsApp, Telegram) based on settings."""
    from app.database import SessionLocal
    from app.models.config import SystemConfig
    from app.notifications.whatsapp import WhatsAppNotifier
    from app.notifications.telegram import TelegramNotifier
    
    channel = "telegram"  # default fallback
    try:
        db = SessionLocal()
        try:
            cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "notification_channel",
                SystemConfig.organization_id == organization_id
            ).first()
            # fallback to global if None and organization_id is set
            if not cfg and organization_id:
                cfg = db.query(SystemConfig).filter(
                    SystemConfig.key == "notification_channel",
                    SystemConfig.organization_id == None
                ).first()
            if cfg and cfg.value:
                channel = cfg.value.strip().lower()
        finally:
            db.close()
    except Exception:
        pass
        
    if channel == "whatsapp":
        return WhatsAppNotifier(organization_id=organization_id)
    # Default fallback to Telegram
    return TelegramNotifier(organization_id=organization_id)
