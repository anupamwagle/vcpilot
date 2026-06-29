"""Import all models here so SQLAlchemy registers them with Base.metadata."""
from app.models.account import Account, AccountTier, Organization, OrganizationTier  # noqa
from app.models.auth import User, Role, Permission, OrganizationMembership            # noqa
from app.models.config import SystemConfig, RuleConfig                                # noqa
from app.models.market import Stock, PriceBar, EntryCheckLog, StockFundamentals       # noqa
from app.models.signal import Signal, Watchlist, WatchlistLabel                       # noqa
from app.models.trade import Trade, Position, Order                                   # noqa
from app.models.audit import AuditLog                                                 # noqa
from app.models.exchange import ExchangeConfig, MarketRegimeRecord                    # noqa
from app.models.mcp import MCPCredential                                               # noqa

all_models = [
    Account, AccountTier, Organization,
    User, Role, Permission, OrganizationMembership,
    SystemConfig, RuleConfig,
    Stock, PriceBar, EntryCheckLog, StockFundamentals,
    Signal, Watchlist, WatchlistLabel,
    Trade, Position, Order,
    AuditLog,
    ExchangeConfig, MarketRegimeRecord,
    MCPCredential,
]
