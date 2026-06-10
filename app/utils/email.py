"""
AstraTrade — Email Notification Utility
Handles sending HTML emails (OTPs, password reset tokens) via smtplib.
"""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from loguru import logger
from app.config import settings


def send_email(to_email: str, subject: str, html_content: str) -> bool:
    """Send an HTML email using SMTP parameters from config/environment."""
    if not settings.smtp_host:
        logger.warning("SMTP_HOST is not configured. Skipping email send.")
        return False

    try:
        # Construct message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
        msg["To"] = to_email

        # Attach HTML part
        msg.attach(MIMEText(html_content, "html"))

        # Connect to SMTP server
        logger.debug(f"Connecting to SMTP server {settings.smtp_host}:{settings.smtp_port}...")
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)

        # Start TLS connection if active
        if settings.smtp_use_tls:
            server.starttls()

        # Log in if credentials are provided
        if settings.smtp_username and settings.smtp_password:
            server.login(settings.smtp_username, settings.smtp_password)

        # Send email
        server.sendmail(settings.smtp_from_email, to_email, msg.as_string())
        server.quit()
        logger.info(f"Email sent successfully to {to_email} with subject: {subject}")
        return True

    except Exception as e:
        logger.error(f"Failed to send email to {to_email} via SMTP: {e}")
        return False
