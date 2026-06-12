"""
AstraTrade Celery Application.
Broker: Redis. All tasks are registered here and imported by workers.

Beat schedule file: /tmp/celerybeat-schedule (always writable — no permission issues).
Beat command: celery -A app.tasks.celery_app beat --loglevel=info
              --schedule=/tmp/celerybeat-schedule --pidfile=/tmp/celerybeat.pid
              --max-interval=30
"""
from celery import Celery
from celery.schedules import crontab, timedelta
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
        "app.tasks.screening.*": {"queue": "screening_equities"},
        "app.tasks.trading.*":   {"queue": "trading_equities"},
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
            "options": {"queue": "screening_equities"},
        },

        # =================================================================
        # Morning ASX data safety-net refresh (8am Mon-Fri)
        # Ensures watchlist/signals reflect the latest EOD bars even if the
        # primary 5pm refresh failed or the worker was down overnight.
        # Skips gracefully on non-trading days (calendar gate inside task).
        # =================================================================
        "refresh-price-data-morning": {
            "task": "app.tasks.screening.refresh_price_data",
            "schedule": crontab(hour=8, minute=0, day_of_week="mon-fri"),
            "options": {"queue": "screening_equities"},
        },

        # =================================================================
        # AstraTrade screener (runs after data refresh at 5:30pm)
        # =================================================================
        "run-screener": {
            "task": "app.tasks.screening.run_daily_screen",
            "schedule": crontab(hour=17, minute=30, day_of_week="mon-fri"),
            "options": {"queue": "screening_equities"},
        },

        # =================================================================
        # Market regime evaluation (runs at 5:15pm with fresh data)
        # =================================================================
        "evaluate-market-regime": {
            "task": "app.tasks.screening.evaluate_market_regime_task",
            "schedule": crontab(hour=17, minute=15, day_of_week="mon-fri"),
            "options": {"queue": "screening_equities"},
        },

        # =================================================================
        # Intraday entry trigger check (every 5 min during ASX hours)
        # ASX: 10:00am–4:12pm AEST
        # =================================================================
        "check-entry-triggers": {
            "task": "app.tasks.trading.check_entry_triggers",
            "schedule": crontab(hour="10-16", minute="*/5", day_of_week="mon-fri"),
            "options": {"queue": "trading_equities"},
        },

        # =================================================================
        # Exit rule evaluation (every 5 min during ASX hours)
        # =================================================================
        "check-exit-rules": {
            "task": "app.tasks.trading.check_exit_rules_task",
            "schedule": crontab(hour="10-16", minute="*/5", day_of_week="mon-fri"),
            "options": {"queue": "trading_equities"},
        },

        # =================================================================
        # Position synchronization with IBKR (every 5 min during hours)
        # =================================================================
        "sync-positions": {
            "task": "app.tasks.trading.sync_ibkr_positions_task",
            "schedule": crontab(hour="10-16", minute="*/5", day_of_week="mon-fri"),
            "options": {"queue": "trading_equities"},
        },

        # =================================================================
        # Stop loss sync with IBKR (every 15 min during hours)
        # =================================================================
        "sync-stops": {
            "task": "app.tasks.trading.sync_stop_orders",
            "schedule": crontab(hour="10-16", minute="*/15", day_of_week="mon-fri"),
            "options": {"queue": "trading_equities"},
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
        # Telegram polling — fetches incoming messages for all orgs
        # that have telegram_enabled=true. No HTTPS required.
        # Runs every 10 seconds via timedelta schedule.
        # =================================================================
        "poll-telegram": {
            "task": "app.tasks.reporting.poll_telegram_updates",
            "schedule": timedelta(seconds=10),
            "options": {"queue": "reporting"},
        },

        # =================================================================
        # Weekly universe refresh (Sunday 8pm — updates ASX200 constituents)
        # =================================================================
        "refresh-universe": {
            "task": "app.tasks.screening.refresh_universe",
            "schedule": crontab(hour=20, minute=0, day_of_week="sun"),
            "options": {"queue": "screening_equities"},
        },

        # =================================================================
        # US MARKET — NYSE/NASDAQ
        # NYSE: 9:30am–4:00pm ET = ~11:30pm–6:00am AEST (next day)
        # All times below in AEST/AEDT (Sydney timezone).
        # Note: AEDT (UTC+11) is active Oct–Apr; AEST (UTC+10) May–Sep.
        # Use hour ranges that cover both offsets conservatively.
        # =================================================================

        # Price data refresh after NYSE close (~6am AEST Tue–Sat)
        "refresh-price-data-us": {
            "task": "app.tasks.screening.refresh_price_data",
            "schedule": crontab(hour=7, minute=0, day_of_week="tue,wed,thu,fri,sat"),
            "kwargs": {"exchange_key": "NYSE"},
            "options": {"queue": "screening_equities"},
        },

        # US screener (after US data refresh ~7:30am AEST Tue–Sat)
        "run-screener-us": {
            "task": "app.tasks.screening.run_daily_screen",
            "schedule": crontab(hour=7, minute=30, day_of_week="tue,wed,thu,fri,sat"),
            "kwargs": {"exchange_key": "NYSE"},
            "options": {"queue": "screening_equities"},
        },

        # US market regime (after data refresh ~7:15am AEST)
        "evaluate-market-regime-us": {
            "task": "app.tasks.screening.evaluate_market_regime_task",
            "schedule": crontab(hour=7, minute=15, day_of_week="tue,wed,thu,fri,sat"),
            "kwargs": {"exchange_key": "NYSE"},
            "options": {"queue": "screening_equities"},
        },

        # US intraday entry triggers (every 5 min during NYSE hours, ~11:30pm–6:05am AEST)
        # Cover hour range 23–5 (UTC+10); Beat uses AEST so this fires across midnight
        "check-entry-triggers-us-evening": {
            "task": "app.tasks.trading.check_entry_triggers",
            "schedule": crontab(hour="23", minute="*/5", day_of_week="mon-fri"),
            "kwargs": {"exchange_key": "NYSE"},
            "options": {"queue": "trading_equities"},
        },
        "check-entry-triggers-us-morning": {
            "task": "app.tasks.trading.check_entry_triggers",
            "schedule": crontab(hour="0,1,2,3,4,5,6", minute="*/5", day_of_week="tue,wed,thu,fri,sat"),
            "kwargs": {"exchange_key": "NYSE"},
            "options": {"queue": "trading_equities"},
        },

        # US exit rules (same window as entry triggers)
        "check-exit-rules-us-evening": {
            "task": "app.tasks.trading.check_exit_rules_task",
            "schedule": crontab(hour="23", minute="*/5", day_of_week="mon-fri"),
            "kwargs": {"exchange_key": "NYSE"},
            "options": {"queue": "trading_equities"},
        },
        "check-exit-rules-us-morning": {
            "task": "app.tasks.trading.check_exit_rules_task",
            "schedule": crontab(hour="0,1,2,3,4,5,6", minute="*/5", day_of_week="tue,wed,thu,fri,sat"),
            "kwargs": {"exchange_key": "NYSE"},
            "options": {"queue": "trading_equities"},
        },

        # =================================================================
        # CRYPTO — 24/7 trading (5-min entry/exit checks, live P&L refresh)
        # =================================================================
        "check-entry-triggers-crypto": {
            "task": "app.tasks.trading.check_entry_triggers",
            "schedule": crontab(minute="*/5"),    # every 5 min, 24/7
            "kwargs": {"exchange_key": "CRYPTO"},
            "options": {"queue": "trading_crypto"},
        },
        "check-exit-rules-crypto": {
            "task": "app.tasks.trading.check_exit_rules_task",
            "schedule": crontab(minute="*/5"),    # every 5 min, 24/7
            "kwargs": {"exchange_key": "CRYPTO"},
            "options": {"queue": "trading_crypto"},
        },
        # Stop sync + ATR trailing stop — every 5 min, 24/7
        "sync-stop-orders-crypto": {
            "task": "app.tasks.trading.sync_stop_orders",
            "schedule": crontab(minute="*/5"),
            "options": {"queue": "trading_crypto"},
        },
        # Live P&L refresh — every 5 min for all exchanges (keeps UI current)
        "update-position-pnl": {
            "task": "app.tasks.trading.update_position_pnl_task",
            "schedule": crontab(minute="*/5"),
            "options": {"queue": "trading_equities"},
        },
        # Crypto data refresh — every 6 hours, 24/7 (price bars stay fresh)
        "refresh-price-data-crypto": {
            "task": "app.tasks.screening.refresh_price_data",
            "schedule": crontab(hour="0,6,12,18", minute=30),
            "kwargs": {"exchange_key": "CRYPTO"},
            "options": {"queue": "screening_equities"},
        },
        # Crypto screener — 4× daily: after each data refresh (catches intraday VCP completions)
        "run-screen-crypto-midnight": {
            "task": "app.tasks.screening.run_daily_screen",
            "schedule": crontab(hour=0, minute=45),
            "kwargs": {"exchange_key": "CRYPTO"},
            "options": {"queue": "screening_equities"},
        },
        "run-screen-crypto-6am": {
            "task": "app.tasks.screening.run_daily_screen",
            "schedule": crontab(hour=6, minute=45),
            "kwargs": {"exchange_key": "CRYPTO"},
            "options": {"queue": "screening_equities"},
        },
        "run-screen-crypto-noon": {
            "task": "app.tasks.screening.run_daily_screen",
            "schedule": crontab(hour=12, minute=45),
            "kwargs": {"exchange_key": "CRYPTO"},
            "options": {"queue": "screening_equities"},
        },
        "run-screen-crypto-6pm": {
            "task": "app.tasks.screening.run_daily_screen",
            "schedule": crontab(hour=18, minute=45),
            "kwargs": {"exchange_key": "CRYPTO"},
            "options": {"queue": "screening_equities"},
        },
    },
)
