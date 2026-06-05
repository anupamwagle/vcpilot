"""
VCPilot — SaaS/Multi-tenant Database Migration Script
Applies schema updates (adding organization_id), creates new tables, and seeds defaults.
Run via: python3 -m scripts.migrate_saas
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger
from sqlalchemy import text
from app.database import engine, get_db
from app.models.auth import hash_password
from app.config import settings


def migrate():
    logger.info("Starting SaaS/Multi-tenant database migration...")

    with engine.connect() as conn:
        # 1. Create organizations table first (so foreign keys resolve)
        logger.info("Creating 'organizations' table...")
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS organizations (
                id SERIAL PRIMARY KEY,
                name VARCHAR(128) UNIQUE NOT NULL,
                tier VARCHAR(32) NOT NULL DEFAULT 'BRONZE',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW()),
                updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW())
            );
        """))
        conn.commit()

        # 2. Add organization_id column to existing tables if not present
        tables_to_migrate = [
            ("accounts", "organizations(id)"),
            ("signals", "organizations(id)"),
            ("watchlist", "organizations(id)"),
            ("positions", "organizations(id)"),
            ("trades", "organizations(id)"),
            ("orders", "organizations(id)"),
            ("system_configs", "organizations(id)"),
            ("audit_logs", "organizations(id)"),
        ]

        for table, ref in tables_to_migrate:
            # Check if column exists
            res = conn.execute(text(f"""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = '{table}' AND column_name = 'organization_id';
            """)).fetchone()

            if not res:
                logger.info(f"Adding 'organization_id' column to table '{table}'...")
                conn.execute(text(f"""
                    ALTER TABLE {table} 
                    ADD COLUMN organization_id INTEGER REFERENCES {ref} ON DELETE CASCADE;
                """))
                conn.commit()
            else:
                logger.debug(f"Column 'organization_id' already exists in '{table}'")

        # 3. Modify system_configs unique constraint
        logger.info("Updating unique constraints on 'system_configs'...")
        try:
            conn.execute(text("ALTER TABLE system_configs DROP CONSTRAINT IF EXISTS system_configs_key_key CASCADE;"))
            conn.execute(text("DROP INDEX IF EXISTS system_configs_key_key;"))
            conn.execute(text("DROP INDEX IF EXISTS ix_system_configs_key CASCADE;"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_system_configs_key ON system_configs (key);"))
            
            # Check if composite unique constraint already exists
            res = conn.execute(text("""
                SELECT constraint_name 
                FROM information_schema.table_constraints 
                WHERE table_name = 'system_configs' AND constraint_name = 'uq_system_config_key_org';
            """)).fetchone()

            if not res:
                conn.execute(text("""
                    ALTER TABLE system_configs 
                    ADD CONSTRAINT uq_system_config_key_org UNIQUE (key, organization_id);
                """))
                logger.info("Added composite unique constraint uq_system_config_key_org")
            conn.commit()
        except Exception as e:
            logger.warning(f"Could not update system_configs unique constraint (might already be configured): {e}")

        # 4. Create RBAC tables
        logger.info("Creating RBAC tables...")
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS permissions (
                id SERIAL PRIMARY KEY,
                name VARCHAR(64) UNIQUE NOT NULL,
                description VARCHAR(256)
            );
            CREATE TABLE IF NOT EXISTS roles (
                id SERIAL PRIMARY KEY,
                name VARCHAR(64) UNIQUE NOT NULL,
                description VARCHAR(256)
            );
            CREATE TABLE IF NOT EXISTS role_permissions (
                role_id INTEGER REFERENCES roles(id) ON DELETE CASCADE,
                permission_id INTEGER REFERENCES permissions(id) ON DELETE CASCADE,
                PRIMARY KEY (role_id, permission_id)
            );
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email VARCHAR(128) UNIQUE NOT NULL,
                password_hash VARCHAR(256) NOT NULL,
                name VARCHAR(128),
                organization_id INTEGER REFERENCES organizations(id) ON DELETE CASCADE NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                otp_code VARCHAR(32),
                otp_expires_at TIMESTAMP WITHOUT TIME ZONE,
                reset_token VARCHAR(128),
                reset_token_expires TIMESTAMP WITHOUT TIME ZONE,
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW()),
                updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW())
            );
            CREATE TABLE IF NOT EXISTS user_roles (
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                role_id INTEGER REFERENCES roles(id) ON DELETE CASCADE,
                PRIMARY KEY (user_id, role_id)
            );
        """))
        conn.commit()

        # Add OTP & Reset columns to users table if they don't exist yet
        user_columns_to_add = [
            ("otp_code", "VARCHAR(32)"),
            ("otp_expires_at", "TIMESTAMP WITHOUT TIME ZONE"),
            ("reset_token", "VARCHAR(128)"),
            ("reset_token_expires", "TIMESTAMP WITHOUT TIME ZONE")
        ]
        for col_name, col_type in user_columns_to_add:
            col_exists = conn.execute(text(f"""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'users' AND column_name = '{col_name}';
            """)).fetchone()
            if not col_exists:
                logger.info(f"Adding '{col_name}' column to 'users' table...")
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col_name} {col_type};"))
                conn.commit()

        # Add user_id column to audit_logs if not exists
        audit_user_col = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'audit_logs' AND column_name = 'user_id';
        """)).fetchone()
        if not audit_user_col:
            logger.info("Adding 'user_id' column to 'audit_logs'...")
            conn.execute(text("""
                ALTER TABLE audit_logs
                ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
            """))
            conn.commit()

        # Add organization_id column to rule_configs if not exists
        rule_col_exists = conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'rule_configs' AND column_name = 'organization_id';
        """)).fetchone()
        if not rule_col_exists:
            logger.info("Adding 'organization_id' column to 'rule_configs' table...")
            conn.execute(text("ALTER TABLE rule_configs ADD COLUMN organization_id INTEGER REFERENCES organizations(id) ON DELETE CASCADE;"))
            conn.commit()

        # Drop old unique index/constraint on rule_configs.rule_id
        logger.info("Updating unique constraints on 'rule_configs'...")
        try:
            conn.execute(text("ALTER TABLE rule_configs DROP CONSTRAINT IF EXISTS rule_configs_rule_id_key CASCADE;"))
            conn.execute(text("DROP INDEX IF EXISTS ix_rule_configs_rule_id CASCADE;"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_rule_configs_rule_id ON rule_configs (rule_id);"))
            
            # Create conditional unique indexes for multi-tenant isolation
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_rule_config_rule_null_org 
                ON rule_configs (rule_id) 
                WHERE organization_id IS NULL;
            """))
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_rule_config_rule_org 
                ON rule_configs (rule_id, organization_id) 
                WHERE organization_id IS NOT NULL;
            """))
            conn.commit()
        except Exception as e:
            logger.warning(f"Could not update rule_configs unique constraints: {e}")

        # 5. Create Default Organization if none exists
        logger.info("Seeding Default Organization...")
        res = conn.execute(text("SELECT id FROM organizations WHERE name = 'Default Org';")).fetchone()
        if not res:
            conn.execute(text("""
                INSERT INTO organizations (name, tier, is_active)
                VALUES ('Default Org', 'GOLD', TRUE);
            """))
            conn.commit()
            default_org_id = conn.execute(text("SELECT id FROM organizations WHERE name = 'Default Org';")).fetchone()[0]
            logger.info(f"Created 'Default Org' with ID: {default_org_id}")
        else:
            default_org_id = res[0]
            logger.debug(f"'Default Org' exists with ID: {default_org_id}")

        # 6. Map existing NULL organization_ids to Default Org
        logger.info("Mapping existing data to 'Default Org'...")
        for table, _ in tables_to_migrate:
            if table == "system_configs":
                # For system configs, only map the tenant-specific ones; keep global ones (e.g. heartbeat, regime) NULL
                conn.execute(text(f"""
                    UPDATE system_configs 
                    SET organization_id = :org_id 
                    WHERE organization_id IS NULL 
                      AND key NOT IN ('last_market_regime', 'last_regime_check', 'last_heartbeat',
                                      'mock_time_enabled', 'mock_current_time', 'ibkr_simulate', 'mock_market_regime');
                """), {"org_id": default_org_id})
            else:
                conn.execute(text(f"""
                    UPDATE {table} 
                    SET organization_id = :org_id 
                    WHERE organization_id IS NULL;
                """), {"org_id": default_org_id})
        conn.commit()

        # 7. Seed Permissions
        logger.info("Seeding default permissions...")
        permissions = [
            ("view_trading", "View trading statistics, signals, positions, and watchlist."),
            ("trade", "Perform trading actions like skipping signals and manual overrides."),
            ("view_admin", "View admin dashboard page, heartbeats, logs, and system tasks."),
            ("manage_config", "Change system settings and parameters."),
        ]
        for name, desc in permissions:
            conn.execute(text("""
                INSERT INTO permissions (name, description)
                VALUES (:name, :desc)
                ON CONFLICT (name) DO NOTHING;
            """), {"name": name, "desc": desc})
        conn.commit()

        # 8. Seed Roles
        logger.info("Seeding default roles...")
        roles = [
            ("Super Admin", "Global system administrator with full access across all organisations."),
            ("Organisation Admin", "Full management and trading privileges for the organization."),
            ("Trader", "Can view and trade within the organization, but cannot change settings."),
            ("Viewer", "Read-only access to the organization's dashboard."),
        ]
        for name, desc in roles:
            conn.execute(text("""
                INSERT INTO roles (name, description)
                VALUES (:name, :desc)
                ON CONFLICT (name) DO NOTHING;
            """), {"name": name, "desc": desc})
        conn.commit()

        # 9. Link permissions to roles
        logger.info("Linking permissions to roles...")
        # Get role/permission IDs
        super_role_id = conn.execute(text("SELECT id FROM roles WHERE name = 'Super Admin';")).fetchone()[0]
        admin_role_id = conn.execute(text("SELECT id FROM roles WHERE name = 'Organisation Admin';")).fetchone()[0]
        trader_role_id = conn.execute(text("SELECT id FROM roles WHERE name = 'Trader';")).fetchone()[0]
        viewer_role_id = conn.execute(text("SELECT id FROM roles WHERE name = 'Viewer';")).fetchone()[0]

        view_perm_id = conn.execute(text("SELECT id FROM permissions WHERE name = 'view_trading';")).fetchone()[0]
        trade_perm_id = conn.execute(text("SELECT id FROM permissions WHERE name = 'trade';")).fetchone()[0]
        admin_perm_id = conn.execute(text("SELECT id FROM permissions WHERE name = 'view_admin';")).fetchone()[0]
        config_perm_id = conn.execute(text("SELECT id FROM permissions WHERE name = 'manage_config';")).fetchone()[0]

        # Super Admin and Organisation Admin get everything
        for rid in [super_role_id, admin_role_id]:
            for pid in [view_perm_id, trade_perm_id, admin_perm_id, config_perm_id]:
                conn.execute(text("""
                    INSERT INTO role_permissions (role_id, permission_id)
                    VALUES (:rid, :pid)
                    ON CONFLICT DO NOTHING;
                """), {"rid": rid, "pid": pid})

        # Trader gets view + trade + view_admin (but not config)
        for pid in [view_perm_id, trade_perm_id, admin_perm_id]:
            conn.execute(text("""
                INSERT INTO role_permissions (role_id, permission_id)
                VALUES (:rid, :pid)
                ON CONFLICT DO NOTHING;
            """), {"rid": trader_role_id, "pid": pid})

        # Viewer gets view only
        conn.execute(text("""
            INSERT INTO role_permissions (role_id, permission_id)
            VALUES (:rid, :pid)
            ON CONFLICT DO NOTHING;
        """), {"rid": viewer_role_id, "pid": view_perm_id})
        conn.commit()

        # 10. Seed default Organisation Admin user
        logger.info("Seeding default Organisation Admin user...")
        # Get dashboard password or default to vcpilot_2026
        raw_pass = settings.dashboard_password or "vcpilot_2026"
        hashed = hash_password(raw_pass)

        # Check if user already exists
        user_res = conn.execute(text("SELECT id FROM users WHERE email = 'admin@vcpilot.com';")).fetchone()
        if not user_res:
            conn.execute(text("""
                INSERT INTO users (email, password_hash, name, organization_id, is_active)
                VALUES ('admin@vcpilot.com', :hash, 'Default Admin', :org_id, TRUE);
            """), {"hash": hashed, "org_id": default_org_id})
            conn.commit()
            uid = conn.execute(text("SELECT id FROM users WHERE email = 'admin@vcpilot.com';")).fetchone()[0]
            logger.info("Created default Organisation Admin user 'admin@vcpilot.com'")
        else:
            uid = user_res[0]
            logger.debug("Default Organisation Admin user already exists")

        # Assign Organisation Admin role to user
        conn.execute(text("""
            INSERT INTO user_roles (user_id, role_id)
            VALUES (:uid, :rid)
            ON CONFLICT DO NOTHING;
        """), {"uid": uid, "rid": admin_role_id})

        # ── Watchlist labels table ─────────────────────────────────────────────
        logger.info("Creating 'watchlist_labels' table...")
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS watchlist_labels (
                id SERIAL PRIMARY KEY,
                organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                name VARCHAR(64) NOT NULL,
                color VARCHAR(16) NOT NULL DEFAULT '#f59e0b',
                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW())
            );
            CREATE INDEX IF NOT EXISTS ix_watchlist_labels_org ON watchlist_labels (organization_id);
        """))
        conn.commit()

        # Add label_id column to watchlist if missing
        lbl_col = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'watchlist' AND column_name = 'label_id';
        """)).fetchone()
        if not lbl_col:
            logger.info("Adding 'label_id' column to 'watchlist'...")
            conn.execute(text("""
                ALTER TABLE watchlist
                ADD COLUMN label_id INTEGER REFERENCES watchlist_labels(id) ON DELETE SET NULL;
            """))
            conn.commit()

        # Add rule_overrides column to signals if missing
        sig_ovr_col = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'signals' AND column_name = 'rule_overrides';
        """)).fetchone()
        if not sig_ovr_col:
            logger.info("Adding 'rule_overrides' column to 'signals'...")
            conn.execute(text("ALTER TABLE signals ADD COLUMN rule_overrides JSONB DEFAULT '{}'::jsonb;"))
            conn.commit()

        # Ensure all organizations have the required system config keys
        configs_to_ensure = [
            ("trading_paused", "false", "BOOLEAN", "Trading Paused", "Toggles automated trade placement", "general", False),
            ("whatsapp_enabled", "true", "BOOLEAN", "WhatsApp Alerts", "Enables real-time notifications", "whatsapp", False),
            ("whatsapp_admin_number", "", "STRING", "WhatsApp Admin Number", "Number to send alerts and receive commands JID format", "whatsapp", False),
            ("whatsapp_api_key", settings.waha_api_key, "STRING", "WhatsApp API Key", "API key for the WhatsApp (WAHA) service", "whatsapp", True),
            ("whatsapp_session_name", "default", "STRING", "WhatsApp Session Name", "WAHA session name (always \'default\' for WAHA Core)", "whatsapp", False),
            ("ibkr_account", "", "STRING", "IBKR Account ID", "Interactive Brokers account number", "broker", False),
            ("ibkr_username", "", "STRING", "IBKR Username", "Interactive Brokers login username", "broker", False),
            ("ibkr_password", "", "STRING", "IBKR Password", "Interactive Brokers login password", "broker", True),
            ("ibkr_paper_mode", "true", "BOOLEAN", "IBKR Paper Mode", "Use paper trading environment", "broker", False),
            ("fmp_api_key", "", "STRING", "FMP API Key", "Financial Modeling Prep API key", "general", True),
            ("working_capital_aud", "5000.0", "FLOAT", "Working Capital (AUD)", "Working capital used for sizing and risk calculations", "general", False),
            ("org_timezone", "Australia/Sydney", "STRING", "Display Timezone",
             "IANA timezone for displaying timestamps (e.g. UTC, Australia/Sydney). "
             "Beat schedules always run on AEST since ASX is in Sydney.", "general", False),
        ]
        try:
            conn.execute(text("DELETE FROM system_configs WHERE key = 'weekly_injection_aud';"))
            conn.commit()
        except Exception:
            pass
        orgs_res = conn.execute(text("SELECT id FROM organizations;")).fetchall()
        for org in orgs_res:
            org_id = org[0]

            # Ensure config keys exist
            for key, val, vtype, label, desc, group, is_secret in configs_to_ensure:
                existing_cfg = conn.execute(text("""
                    SELECT 1 FROM system_configs
                    WHERE key = :key AND organization_id = :org_id;
                """), {"key": key, "org_id": org_id}).fetchone()
                if not existing_cfg:
                    logger.info(f"Seeding missing config '{key}' for organization ID {org_id}...")
                    conn.execute(text("""
                        INSERT INTO system_configs (key, value, value_type, label, description, organization_id, "group", is_secret)
                        VALUES (:key, :value, :value_type, :label, :description, :org_id, :group, :is_secret);
                    """), {
                        "key": key, "value": val, "value_type": vtype, "label": label,
                        "description": desc, "org_id": org_id, "group": group, "is_secret": is_secret
                    })

            # Fix org_timezone label/description for existing rows
            conn.execute(text("""
                UPDATE system_configs
                SET label = 'Display Timezone',
                    description = 'IANA timezone for displaying timestamps. Beat schedules always run on AEST.'
                WHERE key = 'org_timezone' AND organization_id = :org_id;
            """), {"org_id": org_id})
            conn.commit()

            # Seed default watchlist labels for org if none exist
            has_labels = conn.execute(text(
                "SELECT 1 FROM watchlist_labels WHERE organization_id = :org_id LIMIT 1;"
            ), {"org_id": org_id}).fetchone()
            if not has_labels:
                logger.info(f"Seeding default watchlist labels for org {org_id}...")
                default_labels = [
                    ("Favourites", "#f59e0b", True, 0),
                    ("High Priority", "#ef4444", False, 1),
                    ("VCP Forming", "#3b82f6", False, 2),
                    ("Under Review", "#8b5cf6", False, 3),
                ]
                for lname, lcolor, lis_default, lorder in default_labels:
                    conn.execute(text("""
                        INSERT INTO watchlist_labels (organization_id, name, color, is_default, sort_order)
                        VALUES (:org_id, :name, :color, :is_default, :sort_order)
                        ON CONFLICT DO NOTHING;
                    """), {"org_id": org_id, "name": lname, "color": lcolor,
                           "is_default": lis_default, "sort_order": lorder})
                conn.commit()

            # Clone global template rules for org if none exist
            has_rules = conn.execute(text(
                "SELECT 1 FROM rule_configs WHERE organization_id = :org_id LIMIT 1;"
            ), {"org_id": org_id}).fetchone()
            if not has_rules:
                has_templates = conn.execute(text(
                    "SELECT 1 FROM rule_configs WHERE organization_id IS NULL LIMIT 1;"
                )).fetchone()
                if has_templates:
                    logger.info(f"Cloning global template rules for organization ID {org_id}...")
                    conn.execute(text("""
                        INSERT INTO rule_configs (
                            rule_id, organization_id, category, label, description, minervini_ref,
                            enabled_globally, threshold, threshold_label, threshold_min, threshold_max,
                            tier_overrides, is_mandatory, sort_order, updated_by
                        )
                        SELECT
                            rule_id, :org_id, category, label, description, minervini_ref,
                            enabled_globally, threshold, threshold_label, threshold_min, threshold_max,
                            tier_overrides, is_mandatory, sort_order, 'migration'
                        FROM rule_configs
                        WHERE organization_id IS NULL;
                    """), {"org_id": org_id})
                    conn.commit()
        conn.commit()

        # ── Seed global system configs (organization_id IS NULL) ─────────────────
        global_system_configs = [
            ("mock_time_enabled", "false", "BOOLEAN", "Mock Time Enabled",
             "Enable global clock mocking for rule testing", "system"),
            ("mock_current_time", "", "STRING", "Mock Current Time",
             "Simulated datetime in YYYY-MM-DD HH:MM:SS format", "system"),
            ("ibkr_simulate", "false", "BOOLEAN", "IBKR Simulation Mode",
             "When true, orders are simulated locally without sending to IBKR Gateway", "system"),
        ]
        for key, val, vtype, label, desc, group in global_system_configs:
            exists = conn.execute(text(
                "SELECT 1 FROM system_configs WHERE key = :k AND organization_id IS NULL;"
            ), {"k": key}).fetchone()
            if not exists:
                logger.info(f"Seeding global system config '{key}'...")
                conn.execute(text(
                    "INSERT INTO system_configs (key, value, value_type, label, description, \"group\", organization_id) "
                    "VALUES (:key, :val, :vtype, :label, :desc, :group, NULL) ON CONFLICT DO NOTHING;"
                ), {"key": key, "val": val, "vtype": vtype, "label": label, "desc": desc, "group": group})
        conn.commit()

        # ── entry_check_logs table ─────────────────────────────────────────────
        logger.info("Creating 'entry_check_logs' table...")
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS entry_check_logs (
                id              SERIAL PRIMARY KEY,
                organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                signal_id       INTEGER REFERENCES signals(id) ON DELETE SET NULL,
                ticker          VARCHAR(16) NOT NULL,
                checked_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT TIMEZONE('utc', NOW()),
                price_current   NUMERIC(12, 4),
                price_pivot     NUMERIC(12, 4),
                price_stop      NUMERIC(12, 4),
                price_vs_pivot  NUMERIC(8, 4),
                vol_current     BIGINT,
                vol_avg_50      NUMERIC(18, 2),
                vol_ratio       NUMERIC(8, 4),
                ma_10           NUMERIC(12, 4),
                ma_50           NUMERIC(12, 4),
                ma_150          NUMERIC(12, 4),
                ma_200          NUMERIC(12, 4),
                high_52w        NUMERIC(12, 4),
                low_52w         NUMERIC(12, 4),
                pct_from_52w_high NUMERIC(8, 4),
                rs_rating       NUMERIC(6, 2),
                breakout_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
                rule_results    JSONB DEFAULT '{}'::jsonb,
                data_source     VARCHAR(32) DEFAULT 'yfinance',
                data_delay_mins INTEGER DEFAULT 20,
                bar_timestamp   TIMESTAMP WITHOUT TIME ZONE,
                created_at      TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW())
            );
            CREATE INDEX IF NOT EXISTS ix_ecl_org_checked ON entry_check_logs (organization_id, checked_at DESC);
            CREATE INDEX IF NOT EXISTS ix_ecl_ticker      ON entry_check_logs (ticker);
            CREATE INDEX IF NOT EXISTS ix_ecl_signal      ON entry_check_logs (signal_id);
        """))
        conn.commit()

    logger.info("SaaS/Multi-tenant migration and seeding complete!")


if __name__ == "__main__":
    migrate()
