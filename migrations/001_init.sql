-- =============================================================================
-- VCPilot — Initial Database Schema
-- Runs automatically on first PostgreSQL container start
-- TimescaleDB extension is enabled here; price_bars becomes a hypertable
-- =============================================================================

-- Enable TimescaleDB
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- =============================================================================
-- Tables are created by SQLAlchemy on app startup (init_db.py).
-- This file handles: TimescaleDB hypertable setup and indexes
-- that must run AFTER SQLAlchemy creates the base tables.
-- =============================================================================

-- This function is called by the app after tables are created.
-- We define it here so it can be called from init_db.py
CREATE OR REPLACE FUNCTION setup_timescaledb_hypertables()
RETURNS void AS $$
BEGIN
    -- Convert price_bars to a TimescaleDB hypertable if not already
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.hypertables
        WHERE hypertable_name = 'price_bars'
    ) THEN
        PERFORM create_hypertable('price_bars', 'date',
            chunk_time_interval => INTERVAL '3 months',
            if_not_exists => TRUE
        );
        RAISE NOTICE 'price_bars converted to hypertable';
    END IF;

    -- Enable compression on price_bars (compress chunks older than 6 months)
    ALTER TABLE price_bars SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'ticker'
    );

    -- Convert audit_logs to hypertable for fast time-range queries
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.hypertables
        WHERE hypertable_name = 'audit_logs'
    ) THEN
        PERFORM create_hypertable('audit_logs', 'created_at',
            chunk_time_interval => INTERVAL '1 month',
            if_not_exists => TRUE
        );
        RAISE NOTICE 'audit_logs converted to hypertable';
    END IF;
END;
$$ LANGUAGE plpgsql;
