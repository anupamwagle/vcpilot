-- ============================================================================
-- VCPilot Migration 002 — Multi-Market Support (ASX + US Equities + Crypto)
-- Run via: python3 -m scripts.migrate_saas (called automatically on startup)
-- Safe to re-run: all statements use IF NOT EXISTS / DO NOTHING guards.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. exchange_configs — global table managed by super admin
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exchange_configs (
    id                SERIAL PRIMARY KEY,
    exchange_key      VARCHAR(32) UNIQUE NOT NULL,
    display_name      VARCHAR(128) NOT NULL,
    asset_type        VARCHAR(16) NOT NULL DEFAULT 'EQUITY',
    broker_type       VARCHAR(16) NOT NULL DEFAULT 'IBKR',
    is_enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    trading_currency  VARCHAR(8) NOT NULL DEFAULT 'USD',
    flag_emoji        VARCHAR(8),
    calendar_key      VARCHAR(32),
    timezone          VARCHAR(64) NOT NULL DEFAULT 'UTC',
    market_open_utc   VARCHAR(8),
    market_close_utc  VARCHAR(8),
    index_ticker      VARCHAR(32),
    ibkr_exchange     VARCHAR(32),
    ibkr_currency     VARCHAR(8),
    ccxt_provider     VARCHAR(64),
    ccxt_sandbox      BOOLEAN DEFAULT FALSE,
    ticker_suffix     VARCHAR(8),
    yfinance_suffix   VARCHAR(8),
    sort_order        INTEGER DEFAULT 0,
    created_at        TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW()),
    updated_at        TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW())
);

-- ----------------------------------------------------------------------------
-- 2. market_regimes — per-exchange regime history
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_regimes (
    id                SERIAL PRIMARY KEY,
    exchange_key      VARCHAR(32) NOT NULL,
    organization_id   INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
    regime            VARCHAR(16) NOT NULL,
    evaluated_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT TIMEZONE('utc', NOW()),
    index_close       NUMERIC(14,4),
    index_ma200       NUMERIC(14,4),
    breadth_pct       NUMERIC(6,2),
    distribution_days INTEGER,
    rule_results      JSONB DEFAULT '{}',
    created_at        TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW())
);

CREATE INDEX IF NOT EXISTS ix_market_regimes_exchange_org
    ON market_regimes (exchange_key, organization_id);
CREATE INDEX IF NOT EXISTS ix_market_regimes_evaluated_at
    ON market_regimes (evaluated_at DESC);

-- ----------------------------------------------------------------------------
-- 3. stocks — add exchange/currency/asset_type columns
-- ----------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='stocks' AND column_name='exchange_key') THEN
        ALTER TABLE stocks ADD COLUMN exchange_key VARCHAR(32) NOT NULL DEFAULT 'ASX';
        CREATE INDEX IF NOT EXISTS ix_stocks_exchange_key ON stocks (exchange_key);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='stocks' AND column_name='exchange_code') THEN
        -- Populate exchange_code from asx_code for existing rows
        ALTER TABLE stocks ADD COLUMN exchange_code VARCHAR(16);
        UPDATE stocks SET exchange_code = asx_code WHERE exchange_code IS NULL;
        ALTER TABLE stocks ALTER COLUMN exchange_code SET NOT NULL;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='stocks' AND column_name='asset_type') THEN
        ALTER TABLE stocks ADD COLUMN asset_type VARCHAR(16) NOT NULL DEFAULT 'EQUITY';
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='stocks' AND column_name='currency') THEN
        ALTER TABLE stocks ADD COLUMN currency VARCHAR(8) NOT NULL DEFAULT 'AUD';
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='stocks' AND column_name='in_index') THEN
        ALTER TABLE stocks ADD COLUMN in_index BOOLEAN DEFAULT FALSE;
        ALTER TABLE stocks ADD COLUMN index_name VARCHAR(32);
        -- Backfill: existing ASX200 stocks
        UPDATE stocks SET in_index = in_asx200, index_name = 'ASX200' WHERE in_asx200 = TRUE;
    END IF;
END $$;

-- Widen ticker column to support longer crypto symbols
DO $$ BEGIN
    ALTER TABLE stocks ALTER COLUMN ticker TYPE VARCHAR(32);
EXCEPTION WHEN others THEN NULL; END $$;

-- ----------------------------------------------------------------------------
-- 4. price_bars — add exchange_key column
-- ----------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='price_bars' AND column_name='exchange_key') THEN
        ALTER TABLE price_bars ADD COLUMN exchange_key VARCHAR(32) NOT NULL DEFAULT 'ASX';
        CREATE INDEX IF NOT EXISTS ix_pricebar_exchange_date ON price_bars (exchange_key, date);
    END IF;
END $$;

-- Widen ticker column
DO $$ BEGIN
    ALTER TABLE price_bars ALTER COLUMN ticker TYPE VARCHAR(32);
EXCEPTION WHEN others THEN NULL; END $$;

-- ----------------------------------------------------------------------------
-- 5. watchlist — add exchange/currency/asset_type
-- ----------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='watchlist' AND column_name='exchange_key') THEN
        ALTER TABLE watchlist ADD COLUMN exchange_key VARCHAR(32) NOT NULL DEFAULT 'ASX';
        ALTER TABLE watchlist ADD COLUMN asset_type   VARCHAR(16) NOT NULL DEFAULT 'EQUITY';
        ALTER TABLE watchlist ADD COLUMN currency     VARCHAR(8)  NOT NULL DEFAULT 'AUD';
        CREATE INDEX IF NOT EXISTS ix_watchlist_exchange ON watchlist (exchange_key);
    END IF;
END $$;

DO $$ BEGIN
    ALTER TABLE watchlist ALTER COLUMN ticker TYPE VARCHAR(32);
EXCEPTION WHEN others THEN NULL; END $$;

-- ----------------------------------------------------------------------------
-- 6. signals — add exchange/currency/asset_type + FX rate
-- ----------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='signals' AND column_name='exchange_key') THEN
        ALTER TABLE signals ADD COLUMN exchange_key       VARCHAR(32) NOT NULL DEFAULT 'ASX';
        ALTER TABLE signals ADD COLUMN asset_type         VARCHAR(16) NOT NULL DEFAULT 'EQUITY';
        ALTER TABLE signals ADD COLUMN currency           VARCHAR(8)  NOT NULL DEFAULT 'AUD';
        ALTER TABLE signals ADD COLUMN suggested_size_local NUMERIC(14,2);
        ALTER TABLE signals ADD COLUMN fx_rate_aud        NUMERIC(10,6);
        CREATE INDEX IF NOT EXISTS ix_signals_exchange ON signals (exchange_key);
    END IF;
END $$;

DO $$ BEGIN
    ALTER TABLE signals ALTER COLUMN ticker TYPE VARCHAR(32);
EXCEPTION WHEN others THEN NULL; END $$;

-- Widen existing numeric columns for crypto price ranges
DO $$ BEGIN
    ALTER TABLE signals ALTER COLUMN close_price   TYPE NUMERIC(14,4);
    ALTER TABLE signals ALTER COLUMN pivot_price   TYPE NUMERIC(14,4);
    ALTER TABLE signals ALTER COLUMN stop_price    TYPE NUMERIC(14,4);
    ALTER TABLE signals ALTER COLUMN target_price_1 TYPE NUMERIC(14,4);
    ALTER TABLE signals ALTER COLUMN target_price_2 TYPE NUMERIC(14,4);
EXCEPTION WHEN others THEN NULL; END $$;

-- ----------------------------------------------------------------------------
-- 7. positions — add exchange/currency + FX rates + numeric qty
-- ----------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='positions' AND column_name='exchange_key') THEN
        ALTER TABLE positions ADD COLUMN exchange_key        VARCHAR(32) NOT NULL DEFAULT 'ASX';
        ALTER TABLE positions ADD COLUMN asset_type          VARCHAR(16) NOT NULL DEFAULT 'EQUITY';
        ALTER TABLE positions ADD COLUMN currency            VARCHAR(8)  NOT NULL DEFAULT 'AUD';
        ALTER TABLE positions ADD COLUMN entry_fx_rate       NUMERIC(10,6);
        ALTER TABLE positions ADD COLUMN current_fx_rate     NUMERIC(10,6);
        ALTER TABLE positions ADD COLUMN unrealised_pnl_local NUMERIC(14,2);
        CREATE INDEX IF NOT EXISTS ix_positions_exchange ON positions (exchange_key);
    END IF;
END $$;

DO $$ BEGIN
    ALTER TABLE positions ALTER COLUMN ticker TYPE VARCHAR(32);
    -- Widen price columns for crypto ranges (e.g. BTC at $50,000+)
    ALTER TABLE positions ALTER COLUMN entry_price  TYPE NUMERIC(14,4);
    ALTER TABLE positions ALTER COLUMN current_price TYPE NUMERIC(14,4);
    ALTER TABLE positions ALTER COLUMN initial_stop  TYPE NUMERIC(14,4);
    ALTER TABLE positions ALTER COLUMN current_stop  TYPE NUMERIC(14,4);
    ALTER TABLE positions ALTER COLUMN target_1      TYPE NUMERIC(14,4);
    ALTER TABLE positions ALTER COLUMN target_2      TYPE NUMERIC(14,4);
    ALTER TABLE positions ALTER COLUMN avg_cost      TYPE NUMERIC(14,4);
EXCEPTION WHEN others THEN NULL; END $$;

-- ----------------------------------------------------------------------------
-- 8. trades — add exchange/currency + FX rates + local P&L
-- ----------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='trades' AND column_name='exchange_key') THEN
        ALTER TABLE trades ADD COLUMN exchange_key     VARCHAR(32) NOT NULL DEFAULT 'ASX';
        ALTER TABLE trades ADD COLUMN asset_type       VARCHAR(16) NOT NULL DEFAULT 'EQUITY';
        ALTER TABLE trades ADD COLUMN currency         VARCHAR(8)  NOT NULL DEFAULT 'AUD';
        ALTER TABLE trades ADD COLUMN entry_fx_rate    NUMERIC(10,6);
        ALTER TABLE trades ADD COLUMN exit_fx_rate     NUMERIC(10,6);
        ALTER TABLE trades ADD COLUMN gross_pnl_local  NUMERIC(14,2);
        ALTER TABLE trades ADD COLUMN commission_local NUMERIC(12,4) DEFAULT 0;
        ALTER TABLE trades ADD COLUMN net_pnl_local    NUMERIC(14,2);
        CREATE INDEX IF NOT EXISTS ix_trades_exchange ON trades (exchange_key);
    END IF;
END $$;

DO $$ BEGIN
    ALTER TABLE trades ALTER COLUMN ticker TYPE VARCHAR(32);
    ALTER TABLE trades ALTER COLUMN entry_price TYPE NUMERIC(14,4);
    ALTER TABLE trades ALTER COLUMN exit_price  TYPE NUMERIC(14,4);
    ALTER TABLE trades ALTER COLUMN initial_stop TYPE NUMERIC(14,4);
EXCEPTION WHEN others THEN NULL; END $$;

-- ----------------------------------------------------------------------------
-- 9. orders — add exchange/currency + external_order_id for crypto
-- ----------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='orders' AND column_name='exchange_key') THEN
        ALTER TABLE orders ADD COLUMN exchange_key       VARCHAR(32) NOT NULL DEFAULT 'ASX';
        ALTER TABLE orders ADD COLUMN asset_type         VARCHAR(16) NOT NULL DEFAULT 'EQUITY';
        ALTER TABLE orders ADD COLUMN currency           VARCHAR(8)  NOT NULL DEFAULT 'AUD';
        ALTER TABLE orders ADD COLUMN external_order_id  VARCHAR(128);
        ALTER TABLE orders ADD COLUMN commission_local   NUMERIC(12,4) DEFAULT 0;
        ALTER TABLE orders ADD COLUMN fx_rate_aud        NUMERIC(10,6);
        CREATE INDEX IF NOT EXISTS ix_orders_exchange ON orders (exchange_key);
    END IF;
END $$;

DO $$ BEGIN
    ALTER TABLE orders ALTER COLUMN ticker TYPE VARCHAR(32);
EXCEPTION WHEN others THEN NULL; END $$;

-- ----------------------------------------------------------------------------
-- 10. entry_check_logs — add exchange_key
-- ----------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='entry_check_logs' AND column_name='exchange_key') THEN
        ALTER TABLE entry_check_logs ADD COLUMN exchange_key VARCHAR(32) DEFAULT 'ASX';
    END IF;
END $$;

DO $$ BEGIN
    ALTER TABLE entry_check_logs ALTER COLUMN ticker TYPE VARCHAR(32);
EXCEPTION WHEN others THEN NULL; END $$;

-- ----------------------------------------------------------------------------
-- 11. Seed default ExchangeConfig rows (idempotent)
-- ----------------------------------------------------------------------------
INSERT INTO exchange_configs (
    exchange_key, display_name, asset_type, broker_type,
    is_enabled, trading_currency, flag_emoji,
    calendar_key, timezone, market_open_utc, market_close_utc,
    index_ticker, ibkr_exchange, ibkr_currency,
    ticker_suffix, yfinance_suffix, sort_order
) VALUES
    ('ASX',    'Australian Securities Exchange', 'EQUITY', 'IBKR',
     TRUE,  'AUD', '🇦🇺',
     'ASX', 'Australia/Sydney', '00:00', '06:12',
     '^AXJO', 'ASX', 'AUD',
     '.AX', '.AX', 10),

    ('NYSE',   'New York Stock Exchange',        'EQUITY', 'IBKR',
     TRUE,  'USD', '🇺🇸',
     'NYSE', 'America/New_York', '14:30', '21:00',
     '^GSPC', 'SMART', 'USD',
     NULL, NULL, 20),

    ('NASDAQ', 'NASDAQ',                         'EQUITY', 'IBKR',
     TRUE,  'USD', '🇺🇸',
     'NASDAQ', 'America/New_York', '14:30', '21:00',
     '^IXIC', 'SMART', 'USD',
     NULL, NULL, 30),

    ('CRYPTO_BINANCE', 'Binance',                'CRYPTO', 'CCXT',
     FALSE, 'USDT', '₿',
     NULL, 'UTC', '00:00', '23:59',
     'BTC-USD', NULL, NULL,
     '-USD', '-USD', 40),

    ('CRYPTO_COINBASE', 'Coinbase Advanced Trade','CRYPTO', 'CCXT',
     FALSE, 'USD', '₿',
     NULL, 'UTC', '00:00', '23:59',
     'BTC-USD', NULL, NULL,
     '-USD', '-USD', 50),

    ('CRYPTO_KRAKEN',  'Kraken',                 'CRYPTO', 'CCXT',
     FALSE, 'USD', '₿',
     NULL, 'UTC', '00:00', '23:59',
     'BTC-USD', NULL, NULL,
     '-USD', '-USD', 60)

ON CONFLICT (exchange_key) DO NOTHING;
