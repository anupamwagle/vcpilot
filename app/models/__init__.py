"""Import all models here so SQLAlchemy registers them with Base.metadata."""
from app.models.account import Account, AccountTier         # noqa
from app.models.config import SystemConfig, RuleConfig      # noqa
from app.models.market import Stock, PriceBar               # noqa
from app.models.signal import Signal, Watchlist             # noqa
from app.models.trade import Trade, Position, Order         # noqa
from app.models.audit import AuditLog                       # noqa

all_models = [
    Account, AccountTier,
    SystemConfig, RuleConfig,
    Stock, PriceBar,
    Signal, Watchlist,
    Trade, Position, Order,
    AuditLog,
]
