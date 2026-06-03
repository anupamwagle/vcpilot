"""
VCPilot Celery Application.
Broker: Redis. All tasks are registered here and imported by workers.

Beat schedule file: /tmp/celerybeat-schedule (always writable — no permission issues).
Beat command: celery -A app.tasks.celery_app beat --loglevel=info
              --schedule=/tmp/celerybeat-schedule --pidfile=/tmp/celerybeat.pid
              --max-interval=30
"""
from celery import Celery
from celery.schedules import crontab
from app.config import settings

app = Celery(
    "vcpilot",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.tasks.screening",
        "app.tasks.trading",
        "app.tasks.reporting",
    ],
)

app.conf.update(
    # Serialisation
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Australia/Sydney",
    enable_utc=True,

    # Reliability
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,

    # Queues
    task_routes={
        "app.tasks.screening.*": {"queue": "screening"},
        "app.tasks.trading.*":   {"queue": "trading"},
        "app.tasks.reporting.*": {"queue": "reporting"},
    },
    task_default_queue="default",

    # Result retention
    result_expires=86400,  # 24 hours

    # Beat scheduler — store state in /tmp so any user can write it.
    # This matches the --schedule flag on the beat container command.
    beat_schedule_filename="/tmp/celerybeat-schedule",
    beat_max_loop_interval=30,  # seconds — keeps beat responsive

    # Beat schedule (ASX-aligned, all times AEST/AEDT)
    beat_schedule={
        # =================================================================
        # Daily universe + price data refresh (runs at 5pm after ASX close)
        # =================================================================
        "refresh-price-data": {
            "task": "app.tasks.screening.refresh_price_data",
            "schedule": crontab(hour=17, minute=0, day_of_week="mon-fri"),
            "options": {"queue": "screening"},
        },

        # =================================================================
        # Minervini screener (runs after data refresh at 5:30pm)
        # =================================================================
        "run-screener": {
            "task": "app.tasks.screening.run_daily_screen",
            "schedule": crontab(hour=17, minute=30, day_of_week="mon-fri"),
            "options": {"queue": "screening"},
        },

        # =================================================================
        # Market regime evaluation (runs at 5:15pm with fresh data)
        # =================================================================
        "evaluate-market-regime": {
            "task": "app.tasks.screening.evaluate_market_regime_task",
            "schedule": crontab(hour=17, minute=15, day_of_week="mon-fri"),
            "options": {"queue": "screening"},
        },

        # =================================================================
        # Intraday entry trigger check (every 5 min during ASX hours)
        # ASX: 10:00am–4:12pm AEST
        # =================================================================
        "check-entry-triggers": {
            "task": "app.tasks.trading.check_entry_triggers",
            "schedule": crontab(hour="10-16", minute="*/5", day_of_week="mon-fri"),
            "options": {"queue": "trading"},
        },

        # =================================================================
        # Exit rule evaluation (every 5 min during ASX hours)
        # =================================================================
        "check-exit-rules": {
            "task": "app.tasks.trading.check_exit_rules_task",
            "schedule": crontab(hour="10-16", minute="*/5", day_of_week="mon-fri"),
            "options": {"queue": "trading"},
        },

        # =================================================================
        # Stop loss sync with IBKR (every 15 min during hours)
        # =================================================================
        "sync-stops": {
            "task": "app.tasks.trading.sync_stop_orders",
            "schedule": crontab(hour="10-16", minute="*/15", day_of_week="mon-fri"),
            "options": {"queue": "trading"},
        },

        # =================================================================
        # Daily report (6pm AEST after market close)
        # =================================================================
        "daily-report": {
            "task": "app.tasks.reporting.send_daily_report",
            "schedule": crontab(hour=18, minute=0, day_of_week="mon-fri"),
            "options": {"queue": "reporting"},
        },

        # =================================================================
        # Health check heartbeat (every 10 minutes)
        # =================================================================
        "health-check": {
            "task": "app.tasks.reporting.health_check",
            "schedule": crontab(minute="*/10"),
            "options": {"queue": "default"},
        },

        # =================================================================
        # Weekly universe refresh (Sunday 8pm — updates ASX200 constituents)
        # =================================================================
        "refresh-universe": {
            "task": "app.tasks.screening.refresh_universe",
            "schedule": crontab(hour=20, minute=0, day_of_week="sun"),
            "options": {"queue": "screening"},
        },
    },
)
