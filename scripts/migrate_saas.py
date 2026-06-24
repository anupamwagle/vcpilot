"""
AstraTrade — SaaS/Multi-tenant Database Migration Script
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
        # 0. Alter rulecategory enum if needed (for existing database instances)
        try:
            enum_vals = conn.execute(text("""
                SELECT enumlabel FROM pg_enum 
                JOIN pg_type ON pg_enum.enumtypid = pg_type.oid 
                WHERE pg_type.typname = 'rulecategory';
            """)).fetchall()
            enum_vals = [r[0] for r in enum_vals]
            if enum_vals and "CRYPTO" not in enum_vals:
                logger.info("Adding 'CRYPTO' value to rulecategory enum...")
                conn.commit()
                conn.execute(text("ALTER TYPE rulecategory ADD VALUE 'CRYPTO';"))
        except Exception as e:
            logger.debug(f"Could not verify/alter rulecategory enum: {e}")

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
                                      'mock_time_enabled', 'mock_current_time', 'ibkr_simulate', 'mock_market_regime',
                                      'mcp_base_url');
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
            ("notification_channel", "telegram", "STRING", "Notification Channel", "Active communication channel ('whatsapp' or 'telegram')", "whatsapp", False),
            ("telegram_enabled", "true", "BOOLEAN", "Telegram Alerts Enabled", "Enable or disable Telegram notifications", "whatsapp", False),
            ("telegram_bot_token", "", "STRING", "Telegram Bot Token", "The Telegram Bot Token from @BotFather", "whatsapp", True),
            ("telegram_chat_id", "", "STRING", "Telegram Chat ID", "The Telegram Chat ID to send alerts to", "whatsapp", False),
            ("ibkr_account", "", "STRING", "IBKR Account ID", "Interactive Brokers account number", "broker", False),
            ("novnc_url", "", "STRING", "Gateway Login URL (noVNC)", "noVNC URL for this org's IBKR Gateway. Managed via Super Admin → Org Detail.", "system", False),
            ("vnc_password", "", "STRING", "Gateway VNC Password", "VNC password for this org's IBKR Gateway. Managed via Super Admin → Org Detail.", "system", True),
            ("fmp_api_key", "", "STRING", "FMP API Key", "Financial Modeling Prep API key", "general", True),
            ("working_capital_aud", "5000.0", "FLOAT", "Working Capital (AUD)", "Working capital used for sizing and risk calculations", "general", False),
            ("working_capital_currency", "AUD", "STRING", "Working Capital Currency", "Currency of the working capital (e.g. AUD, USD, USDT, BNB)", "general", False),
            ("weekly_injection_aud", "0", "FLOAT", "Weekly Capital Injection (AUD)", "Amount of capital added to the account each week. Used for compounding position sizing calculations.", "risk", False),
            ("org_timezone", "Australia/Sydney", "STRING", "Display Timezone",
             "IANA timezone for displaying timestamps (e.g. UTC, Australia/Sydney). "
             "Beat schedules always run on AEST since ASX is in Sydney.", "general", False),
        ]
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
                    actual_val = f"org_{org_id}" if key == "whatsapp_session_name" else val
                    conn.execute(text("""
                        INSERT INTO system_configs (key, value, value_type, label, description, organization_id, "group", is_secret)
                        VALUES (:key, :value, :value_type, :label, :description, :org_id, :group, :is_secret);
                    """), {
                        "key": key, "value": actual_val, "value_type": vtype, "label": label,
                        "description": desc, "org_id": org_id, "group": group, "is_secret": is_secret
                    })

            # Fix org_timezone label/description for existing rows
            conn.execute(text("""
                UPDATE system_configs
                SET label = 'Display Timezone',
                    description = 'IANA timezone for displaying timestamps. Beat schedules always run on AEST.'
                WHERE key = 'org_timezone' AND organization_id = :org_id;
            """), {"org_id": org_id})

            # Remove IBKR gateway credentials from SystemConfig — they belong in .env only
            conn.execute(text("""
                DELETE FROM system_configs
                WHERE key IN ('ibkr_username', 'ibkr_password', 'ibkr_paper_mode')
                  AND organization_id = :org_id;
            """), {"org_id": org_id})

            # Move noVNC fields to 'system' group (hidden from org admins)
            conn.execute(text("""
                UPDATE system_configs
                SET "group" = 'system'
                WHERE key IN ('novnc_url', 'vnc_password')
                  AND organization_id = :org_id AND "group" != 'system';
            """), {"org_id": org_id})
            
            # Force update any tenant organization's whatsapp_session_name to match their org ID
            conn.execute(text("""
                UPDATE system_configs
                SET value = 'org_' || organization_id
                WHERE key = 'whatsapp_session_name' AND (value = 'default' OR value = '') AND organization_id = :org_id;
            """), {"org_id": org_id})
            conn.commit()

            # Seed default watchlist labels for org if none exist
            has_labels = conn.execute(text(
                "SELECT 1 FROM watchlist_labels WHERE organization_id = :org_id LIMIT 1;"
            ), {"org_id": org_id}).fetchone()
            if not has_labels:
                logger.info(f"Seeding default watchlist labels for org {org_id}...")
                default_labels = [
                    ("Favourites",    "#f59e0b", True,  0),
                    ("High Priority", "#ef4444", False, 1),
                    ("VCP Forming",   "#3b82f6", False, 2),
                    ("Under Review",  "#8b5cf6", False, 3),
                ]
                for lname, lcolor, lis_default, lorder in default_labels:
                    conn.execute(text("""
                        INSERT INTO watchlist_labels (organization_id, name, color, is_default, sort_order)
                        VALUES (:org_id, :name, :color, :is_default, :sort_order)
                        ON CONFLICT DO NOTHING;
                    """), {"org_id": org_id, "name": lname, "color": lcolor,
                           "is_default": lis_default, "sort_order": lorder})
                conn.commit()

            # Seed asx_universe_scope config key if not present
            conn.execute(text("""
                INSERT INTO system_configs (key, value, value_type, label, "group", description, organization_id)
                VALUES ('asx_universe_scope', 'ASX200', 'STRING', 'ASX Universe Scope', 'trading',
                        'ASX200 = top 200 | ASX300 = top 300 | ALL_LISTED = all ~2200+ companies', :org_id)
                ON CONFLICT DO NOTHING;
            """), {"org_id": org_id})
            conn.commit()

            # Resolve active_exchanges for this org (needed for both ASX and crypto label seeding)
            active_exc_row = conn.execute(text("""
                SELECT value FROM system_configs
                WHERE key = 'active_exchanges' AND organization_id = :org_id LIMIT 1;
            """), {"org_id": org_id}).fetchone()
            active_exc_val = (active_exc_row[0] if active_exc_row else "") or ""

            # Seed ASX sector watchlist labels if org has ASX active
            has_asx_active = "ASX" in active_exc_val.split(",")
            asx_sector_labels = [
                ("Gold",               "#f59e0b", 20),
                ("Lithium",            "#10b981", 21),
                ("Rare Earth",         "#8b5cf6", 22),
                ("Uranium",            "#f97316", 23),
                ("Silver",             "#94a3b8", 24),
                ("Iron & Steel",       "#64748b", 25),
                ("Oil & Gas",          "#dc2626", 26),
                ("Biotech",            "#ec4899", 27),
                ("Healthcare / Pharma","#06b6d4", 28),
                ("FinTech",            "#6366f1", 29),
                ("Technology",         "#3b82f6", 30),
                ("Banks",              "#1e40af", 31),
                ("Financials",         "#1d4ed8", 32),
                ("Real Estate (REIT)", "#7c3aed", 33),
                ("Energy",             "#f97316", 34),
                ("Mining (General)",   "#92400e", 35),
                ("Consumer",           "#16a34a", 36),
                ("Industrials",        "#78716c", 37),
                ("Telco / Media",      "#0891b2", 38),
            ]
            if has_asx_active:
                for lname, lcolor, lorder in asx_sector_labels:
                    exists = conn.execute(text("""
                        SELECT 1 FROM watchlist_labels
                        WHERE organization_id = :org_id AND name = :name LIMIT 1;
                    """), {"org_id": org_id, "name": lname}).fetchone()
                    if not exists:
                        conn.execute(text("""
                            INSERT INTO watchlist_labels (organization_id, name, color, is_default, sort_order)
                            VALUES (:org_id, :name, :color, false, :sort_order);
                        """), {"org_id": org_id, "name": lname, "color": lcolor, "sort_order": lorder})
                conn.commit()

            # Seed crypto-specific watchlist labels if org has a CRYPTO exchange active
            has_crypto_active = any(k.strip().startswith("CRYPTO_") for k in active_exc_val.split(",") if k.strip())
            if has_crypto_active:
                crypto_labels = [
                    ("Crypto Core",  "#06b6d4", False, 10),
                    ("DeFi",         "#10b981", False, 11),
                    ("Altcoins",     "#8b5cf6", False, 12),
                    ("Crypto Watch", "#f97316", False, 13),
                ]
                for lname, lcolor, lis_default, lorder in crypto_labels:
                    exists = conn.execute(text("""
                        SELECT 1 FROM watchlist_labels
                        WHERE organization_id = :org_id AND name = :name LIMIT 1;
                    """), {"org_id": org_id, "name": lname}).fetchone()
                    if not exists:
                        conn.execute(text("""
                            INSERT INTO watchlist_labels (organization_id, name, color, is_default, sort_order)
                            VALUES (:org_id, :name, :color, :is_default, :sort_order);
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
                            tier_overrides, is_mandatory, sort_order, asset_types, updated_by
                        )
                        SELECT
                            rule_id, :org_id, category, label, description, minervini_ref,
                            enabled_globally, threshold, threshold_label, threshold_min, threshold_max,
                            tier_overrides, is_mandatory, sort_order, asset_types, 'migration'
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

        # ── Migration 002 — Multi-Market Support ─────────────────────────────
        # NOTE: Uses Python ALTER TABLE calls (not SQL file) to avoid issues
        # with DO $$ ... $$ blocks that contain embedded semicolons.
        logger.info("Running migration 002 — multi-market support...")

        def _col_exists(tbl, col):
            return conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=:t AND column_name=:c"
            ), {"t": tbl, "c": col}).fetchone() is not None

        def _table_exists(tbl):
            return conn.execute(text(
                "SELECT 1 FROM information_schema.tables WHERE table_name=:t"
            ), {"t": tbl}).fetchone() is not None

        def _safe(stmt):
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.debug(f"Migration 002 stmt skipped: {str(e)[:100]}")

        # 1. exchange_configs table
        if not _table_exists("exchange_configs"):
            logger.info("Creating exchange_configs table...")
            _safe("""
                CREATE TABLE exchange_configs (
                    id SERIAL PRIMARY KEY,
                    exchange_key VARCHAR(32) UNIQUE NOT NULL,
                    display_name VARCHAR(128) NOT NULL,
                    asset_type VARCHAR(16) NOT NULL DEFAULT 'EQUITY',
                    broker_type VARCHAR(16) NOT NULL DEFAULT 'IBKR',
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    trading_currency VARCHAR(8) NOT NULL DEFAULT 'USD',
                    flag_emoji VARCHAR(8),
                    calendar_key VARCHAR(32),
                    timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
                    market_open_utc VARCHAR(8),
                    market_close_utc VARCHAR(8),
                    index_ticker VARCHAR(32),
                    ibkr_exchange VARCHAR(32),
                    ibkr_currency VARCHAR(8),
                    ccxt_provider VARCHAR(64),
                    ccxt_sandbox BOOLEAN DEFAULT FALSE,
                    ticker_suffix VARCHAR(8),
                    yfinance_suffix VARCHAR(8),
                    sort_order INTEGER DEFAULT 0,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW()),
                    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW())
                )
            """)

        # 2. market_regimes table
        if not _table_exists("market_regimes"):
            logger.info("Creating market_regimes table...")
            _safe("""
                CREATE TABLE market_regimes (
                    id SERIAL PRIMARY KEY,
                    exchange_key VARCHAR(32) NOT NULL,
                    organization_id INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
                    regime VARCHAR(16) NOT NULL,
                    evaluated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT TIMEZONE('utc', NOW()),
                    index_close NUMERIC(14,4),
                    index_ma200 NUMERIC(14,4),
                    breadth_pct NUMERIC(6,2),
                    distribution_days INTEGER,
                    rule_results JSONB DEFAULT '{}',
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW())
                )
            """)
            _safe("CREATE INDEX IF NOT EXISTS ix_market_regimes_exchange_org ON market_regimes (exchange_key, organization_id)")
            _safe("CREATE INDEX IF NOT EXISTS ix_market_regimes_evaluated_at ON market_regimes (evaluated_at DESC)")

        # 3. stocks — new columns
        for col, defn in [
            ("exchange_key", "VARCHAR(32) NOT NULL DEFAULT 'ASX'"),
            ("exchange_code", "VARCHAR(16)"),
            ("asset_type",   "VARCHAR(16) NOT NULL DEFAULT 'EQUITY'"),
            ("currency",     "VARCHAR(8) NOT NULL DEFAULT 'AUD'"),
            ("in_index",     "BOOLEAN DEFAULT FALSE"),
            ("index_name",   "VARCHAR(32)"),
        ]:
            if not _col_exists("stocks", col):
                logger.info(f"Adding stocks.{col}...")
                _safe(f"ALTER TABLE stocks ADD COLUMN {col} {defn}")
        # Backfill exchange_code from asx_code
        _safe("UPDATE stocks SET exchange_code = asx_code WHERE exchange_code IS NULL AND asx_code IS NOT NULL")
        _safe("UPDATE stocks SET exchange_code = ticker WHERE exchange_code IS NULL")
        _safe("UPDATE stocks SET in_index = in_asx200, index_name = 'ASX200' WHERE in_asx200 = TRUE AND in_index IS NOT TRUE")
        _safe("ALTER TABLE stocks ALTER COLUMN asx_code DROP NOT NULL")
        _safe("CREATE INDEX IF NOT EXISTS ix_stocks_exchange_key ON stocks (exchange_key)")
        _safe("ALTER TABLE stocks ALTER COLUMN ticker TYPE VARCHAR(32)")

        # 4. price_bars — add exchange_key
        if not _col_exists("price_bars", "exchange_key"):
            logger.info("Adding price_bars.exchange_key...")
            _safe("ALTER TABLE price_bars ADD COLUMN exchange_key VARCHAR(32) NOT NULL DEFAULT 'ASX'")
            _safe("CREATE INDEX IF NOT EXISTS ix_pricebar_exchange_date ON price_bars (exchange_key, date)")
        _safe("ALTER TABLE price_bars ALTER COLUMN ticker TYPE VARCHAR(32)")

        # 5. watchlist — add exchange/currency/asset_type
        for col, defn in [
            ("exchange_key", "VARCHAR(32) NOT NULL DEFAULT 'ASX'"),
            ("asset_type",   "VARCHAR(16) NOT NULL DEFAULT 'EQUITY'"),
            ("currency",     "VARCHAR(8) NOT NULL DEFAULT 'AUD'"),
        ]:
            if not _col_exists("watchlist", col):
                logger.info(f"Adding watchlist.{col}...")
                _safe(f"ALTER TABLE watchlist ADD COLUMN {col} {defn}")
        _safe("CREATE INDEX IF NOT EXISTS ix_watchlist_exchange ON watchlist (exchange_key)")
        _safe("ALTER TABLE watchlist ALTER COLUMN ticker TYPE VARCHAR(32)")

        # 6. signals — add exchange/currency/asset_type + FX
        for col, defn in [
            ("exchange_key",        "VARCHAR(32) NOT NULL DEFAULT 'ASX'"),
            ("asset_type",          "VARCHAR(16) NOT NULL DEFAULT 'EQUITY'"),
            ("currency",            "VARCHAR(8) NOT NULL DEFAULT 'AUD'"),
            ("suggested_size_local","NUMERIC(14,2)"),
            ("fx_rate_aud",         "NUMERIC(10,6)"),
        ]:
            if not _col_exists("signals", col):
                logger.info(f"Adding signals.{col}...")
                _safe(f"ALTER TABLE signals ADD COLUMN {col} {defn}")
        _safe("CREATE INDEX IF NOT EXISTS ix_signals_exchange ON signals (exchange_key)")
        _safe("ALTER TABLE signals ALTER COLUMN ticker TYPE VARCHAR(32)")
        for price_col in ["close_price","pivot_price","stop_price","target_price_1","target_price_2"]:
            _safe(f"ALTER TABLE signals ALTER COLUMN {price_col} TYPE NUMERIC(14,4)")

        # 7. positions — add exchange/currency + FX
        for col, defn in [
            ("exchange_key",         "VARCHAR(32) NOT NULL DEFAULT 'ASX'"),
            ("asset_type",           "VARCHAR(16) NOT NULL DEFAULT 'EQUITY'"),
            ("currency",             "VARCHAR(8) NOT NULL DEFAULT 'AUD'"),
            ("entry_fx_rate",        "NUMERIC(10,6)"),
            ("current_fx_rate",      "NUMERIC(10,6)"),
            ("unrealised_pnl_local", "NUMERIC(14,2)"),
        ]:
            if not _col_exists("positions", col):
                logger.info(f"Adding positions.{col}...")
                _safe(f"ALTER TABLE positions ADD COLUMN {col} {defn}")
        _safe("CREATE INDEX IF NOT EXISTS ix_positions_exchange ON positions (exchange_key)")
        _safe("ALTER TABLE positions ALTER COLUMN ticker TYPE VARCHAR(32)")

        # 8. trades — add exchange/currency + FX + local P&L
        for col, defn in [
            ("exchange_key",    "VARCHAR(32) NOT NULL DEFAULT 'ASX'"),
            ("asset_type",      "VARCHAR(16) NOT NULL DEFAULT 'EQUITY'"),
            ("currency",        "VARCHAR(8) NOT NULL DEFAULT 'AUD'"),
            ("entry_fx_rate",   "NUMERIC(10,6)"),
            ("exit_fx_rate",    "NUMERIC(10,6)"),
            ("gross_pnl_local", "NUMERIC(14,2)"),
            ("commission_local","NUMERIC(12,4) DEFAULT 0"),
            ("net_pnl_local",   "NUMERIC(14,2)"),
        ]:
            if not _col_exists("trades", col):
                logger.info(f"Adding trades.{col}...")
                _safe(f"ALTER TABLE trades ADD COLUMN {col} {defn}")
        _safe("CREATE INDEX IF NOT EXISTS ix_trades_exchange ON trades (exchange_key)")
        _safe("ALTER TABLE trades ALTER COLUMN ticker TYPE VARCHAR(32)")

        # 9. orders — add exchange/currency + external_order_id
        for col, defn in [
            ("exchange_key",      "VARCHAR(32) NOT NULL DEFAULT 'ASX'"),
            ("asset_type",        "VARCHAR(16) NOT NULL DEFAULT 'EQUITY'"),
            ("currency",          "VARCHAR(8) NOT NULL DEFAULT 'AUD'"),
            ("external_order_id", "VARCHAR(128)"),
            ("commission_local",  "NUMERIC(12,4) DEFAULT 0"),
            ("fx_rate_aud",       "NUMERIC(10,6)"),
        ]:
            if not _col_exists("orders", col):
                logger.info(f"Adding orders.{col}...")
                _safe(f"ALTER TABLE orders ADD COLUMN {col} {defn}")
        _safe("CREATE INDEX IF NOT EXISTS ix_orders_exchange ON orders (exchange_key)")
        _safe("ALTER TABLE orders ALTER COLUMN ticker TYPE VARCHAR(32)")

        # 10. entry_check_logs — add exchange_key
        if not _col_exists("entry_check_logs", "exchange_key"):
            _safe("ALTER TABLE entry_check_logs ADD COLUMN exchange_key VARCHAR(32) DEFAULT 'ASX'")
        _safe("ALTER TABLE entry_check_logs ALTER COLUMN ticker TYPE VARCHAR(32)")

        # 11. Seed default ExchangeConfig rows
        logger.info("Seeding ExchangeConfig rows...")
        exchange_seeds = [
            ("ASX",    "Australian Securities Exchange", "EQUITY", "IBKR",  True,  "AUD",  "🇦🇺", "ASX",    "Australia/Sydney",  "00:00", "06:12", "^AXJO",  "ASX",   "AUD",  None,     False, ".AX",  ".AX",  10),
            ("NYSE",   "New York Stock Exchange",        "EQUITY", "IBKR",  True,  "USD",  "🇺🇸", "NYSE",   "America/New_York",  "14:30", "21:00", "^GSPC",  "SMART", "USD",  None,     False, None,   None,   20),
            ("NASDAQ", "NASDAQ",                         "EQUITY", "IBKR",  True,  "USD",  "🇺🇸", "NASDAQ", "America/New_York",  "14:30", "21:00", "^IXIC",  "SMART", "USD",  None,     False, None,   None,   30),
            ("CRYPTO_INDEPENDENTRESERVE", "Independent Reserve",   "CRYPTO", "CCXT", True,  "AUD",  "🇦🇺", None, "Australia/Sydney", "00:00", "23:59", "BTC-AUD", None, None, "independentreserve", False, "-AUD", "-AUD", 40),
            ("CRYPTO_BINANCE",            "Binance",               "CRYPTO", "CCXT", False, "USDT", "₿", None, "UTC", "00:00", "23:59", "BTC-USD", None, None, "binance",            False, "-USD", "-USD", 50),
            ("CRYPTO_COINBASE",           "Coinbase Advanced",     "CRYPTO", "CCXT", False, "USD",  "₿", None, "UTC", "00:00", "23:59", "BTC-USD", None, None, "coinbase",           False, "-USD", "-USD", 60),
            ("CRYPTO_KRAKEN",             "Kraken",                "CRYPTO", "CCXT", False, "USD",  "₿", None, "UTC", "00:00", "23:59", "BTC-USD", None, None, "kraken",             False, "-USD", "-USD", 70),
            ("CRYPTO_MEXC",               "MEXC",                  "CRYPTO", "CCXT", False, "USDT", "₿", None, "UTC", "00:00", "23:59", "BTC-USD", None, None, "mexc",               False, "-USD", "-USD", 80),
        ]
        for row in exchange_seeds:
            (ek, dn, at, bt, ie, tc, fe, ck, tz, mo, mc, it, ie2, ic, cp, cs, ts, ys, so) = row
            exists = conn.execute(text(
                "SELECT 1 FROM exchange_configs WHERE exchange_key = :k"
            ), {"k": ek}).fetchone()
            if not exists:
                conn.execute(text("""
                    INSERT INTO exchange_configs
                    (exchange_key,display_name,asset_type,broker_type,is_enabled,trading_currency,
                     flag_emoji,calendar_key,timezone,market_open_utc,market_close_utc,index_ticker,
                     ibkr_exchange,ibkr_currency,ccxt_provider,ccxt_sandbox,ticker_suffix,yfinance_suffix,sort_order)
                    VALUES (:ek,:dn,:at,:bt,:ie,:tc,:fe,:ck,:tz,:mo,:mc,:it,:ie2,:ic,:cp,:cs,:ts,:ys,:so)
                """), dict(ek=ek,dn=dn,at=at,bt=bt,ie=ie,tc=tc,fe=fe,ck=ck,tz=tz,mo=mo,mc=mc,
                           it=it,ie2=ie2,ic=ic,cp=cp,cs=cs,ts=ts,ys=ys,so=so))
        conn.commit()
        logger.info("Migration 002 complete.")

        # ── Migration 003 — asset_types on RuleConfig ─────────────────────────
        logger.info("Running migration 003 — asset_types on RuleConfig...")
        # Add asset_types column to rule_configs
        at_exists = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'rule_configs' AND column_name = 'asset_types';
        """)).fetchone()
        if not at_exists:
            logger.info("Adding rule_configs.asset_types column...")
            conn.execute(text("""
                ALTER TABLE rule_configs ADD COLUMN asset_types VARCHAR(16) NOT NULL DEFAULT 'BOTH';
            """))
            conn.commit()

        # Backfill EQUITY-only rules
        equity_only = [
            'fundamental_eps_growth_recent', 'fundamental_eps_growth_accel',
            'fundamental_eps_growth_annual', 'fundamental_sales_growth',
            'fundamental_roe', 'fundamental_profit_margin', 'fundamental_institutional_own',
            'regime_pct_stocks_above_200ma', 'regime_distribution_days',
            'entry_sector_leadership', 'exit_earnings_avoid',
            'trend_rs_rating_min',   # RS vs ASX200 has no meaning for crypto
        ]
        for rule_id in equity_only:
            conn.execute(text("""
                UPDATE rule_configs SET asset_types = 'EQUITY'
                WHERE rule_id = :rule_id AND asset_types = 'BOTH';
            """), {"rule_id": rule_id})
        # Widen threshold_max for trend_pct_below_52w_high so crypto users can set 65-75%
        conn.execute(text("""
            UPDATE rule_configs SET threshold_max = 80
            WHERE rule_id = 'trend_pct_below_52w_high' AND (threshold_max IS NULL OR threshold_max < 80);
        """))
        conn.commit()
        logger.info("Migration 003 complete.")

        # ── Migration 004 — numeric column types on RuleConfig ────────────────
        logger.info("Running migration 004 — numeric column types on RuleConfig...")
        _safe("ALTER TABLE rule_configs ALTER COLUMN threshold TYPE NUMERIC(20, 4)")
        _safe("ALTER TABLE rule_configs ALTER COLUMN threshold_min TYPE NUMERIC(20, 4)")
        _safe("ALTER TABLE rule_configs ALTER COLUMN threshold_max TYPE NUMERIC(20, 4)")
        logger.info("Migration 004 complete.")

        # ── Migration 005 — seed Independent Reserve + update Kraken sort_order ─
        logger.info("Running migration 005 — add Independent Reserve exchange...")
        ir_exists = conn.execute(text(
            "SELECT 1 FROM exchange_configs WHERE exchange_key = 'CRYPTO_INDEPENDENTRESERVE'"
        )).fetchone()
        if not ir_exists:
            logger.info("Inserting CRYPTO_INDEPENDENTRESERVE into exchange_configs...")
            conn.execute(text("""
                INSERT INTO exchange_configs
                  (exchange_key, display_name, asset_type, broker_type, is_enabled,
                   trading_currency, flag_emoji, calendar_key, timezone,
                   market_open_utc, market_close_utc, index_ticker,
                   ibkr_exchange, ibkr_currency, ccxt_provider, ccxt_sandbox,
                   ticker_suffix, yfinance_suffix, sort_order)
                VALUES
                  ('CRYPTO_INDEPENDENTRESERVE', 'Independent Reserve', 'CRYPTO', 'CCXT', TRUE,
                   'AUD', '🇦🇺', NULL, 'Australia/Sydney',
                   '00:00', '23:59', 'BTC-AUD',
                   NULL, NULL, 'independentreserve', FALSE,
                   '-AUD', '-AUD', 40)
            """))
        # Ensure IR is enabled and at sort_order 40 (primary crypto exchange)
        conn.execute(text("""
            UPDATE exchange_configs
            SET is_enabled = TRUE, sort_order = 40
            WHERE exchange_key = 'CRYPTO_INDEPENDENTRESERVE'
        """))
        # Update other crypto exchanges sort_order to follow IR
        conn.execute(text("""
            UPDATE exchange_configs SET sort_order = 50
            WHERE exchange_key = 'CRYPTO_BINANCE' AND sort_order < 50
        """))
        conn.commit()
        logger.info("Migration 005 complete.")

        # ── Migration 006 — BEAR regime block rules ───────────────────────────
        logger.info("Running migration 006 — seed regime_bear_block rules...")
        for rule_id, label, description, asset_types in [
            ("regime_bear_block_equities",
             "Block equity entries in BEAR regime",
             "No new equity buy orders when the market regime is BEAR. Disable to allow entries in all market conditions.",
             "EQUITY"),
            ("regime_bear_block_crypto",
             "Block crypto entries in BEAR regime",
             "No new crypto buy orders when the BTC market regime is BEAR. Disable to allow entries regardless of BTC trend.",
             "CRYPTO"),
        ]:
            # Seed global template if missing
            exists_global = conn.execute(text(
                "SELECT 1 FROM rule_configs WHERE rule_id = :r AND organization_id IS NULL"
            ), {"r": rule_id}).fetchone()
            if not exists_global:
                conn.execute(text("""
                    INSERT INTO rule_configs (rule_id, organization_id, category, label, description,
                        minervini_ref, enabled_globally, is_mandatory, sort_order, asset_types, updated_by)
                    VALUES (:rule_id, NULL, 'MARKET_REGIME', :label, :description,
                        'AstraTrade: Only trade in BULL markets', true, false,
                        (SELECT COALESCE(MAX(sort_order), 40) + 1 FROM rule_configs WHERE organization_id IS NULL AND category = 'MARKET_REGIME'),
                        :asset_types, 'migration')
                """), {"rule_id": rule_id, "label": label, "description": description, "asset_types": asset_types})
            # Seed per-org copy for all existing orgs if missing
            orgs_list = conn.execute(text("SELECT id FROM organizations WHERE is_active = true")).fetchall()
            for (oid,) in orgs_list:
                exists_org = conn.execute(text(
                    "SELECT 1 FROM rule_configs WHERE rule_id = :r AND organization_id = :oid"
                ), {"r": rule_id, "oid": oid}).fetchone()
                if not exists_org:
                    conn.execute(text("""
                        INSERT INTO rule_configs (rule_id, organization_id, category, label, description,
                            minervini_ref, enabled_globally, is_mandatory, sort_order, asset_types, updated_by)
                        VALUES (:rule_id, :oid, 'MARKET_REGIME', :label, :description,
                            'AstraTrade: Only trade in BULL markets', true, false,
                            (SELECT COALESCE(MAX(sort_order), 40) + 1 FROM rule_configs WHERE organization_id IS NULL AND category = 'MARKET_REGIME'),
                            :asset_types, 'migration')
                    """), {"rule_id": rule_id, "oid": oid, "label": label, "description": description, "asset_types": asset_types})
        conn.commit()
        logger.info("Migration 006 complete.")

        # ── Migration 007 — Minervini rule tune-up (RS 80, stop cap, trailing exit, earnings cushion) ─
        logger.info("Running migration 007 — Minervini rule optimisation...")

        # 1. Raise RS rating floor 70 → 80 (only where still at the old default, so we
        #    don't clobber a value an admin deliberately set). Widen max to 99.
        conn.execute(text("""
            UPDATE rule_configs
            SET threshold = 80, threshold_max = 99,
                label = 'Relative Strength ≥ 80',
                minervini_ref = 'RS Rating: leaders are 80–90+, not merely ≥ 70'
            WHERE rule_id = 'trend_rs_rating_min' AND threshold = 70;
        """))
        # Ensure the max is wide enough even on rows already bumped
        conn.execute(text("""
            UPDATE rule_configs SET threshold_max = 99
            WHERE rule_id = 'trend_rs_rating_min' AND (threshold_max IS NULL OR threshold_max < 90);
        """))

        # 2. Seed three new rules to the global template AND every org copy.
        #    (rule_id, category, label, description, minervini_ref, asset_types, threshold,
        #     threshold_label, threshold_min, threshold_max, sort_order)
        new_rules = [
            ("equity_stop_width_max_pct", "EXIT_DEFENSIVE",
             "Max equity stop width: 8% below entry",
             "Caps how far the protective stop can sit below the actual entry price. The VCP stop "
             "(low of the final contraction) can occasionally imply a very wide loss; Minervini's "
             "discipline is a 7–8% maximum stop, never beyond 10%, average loss 5–6%. When the natural "
             "stop is wider than this cap it is tightened to the cap (position size rises while honouring "
             "the 2% capital-risk rule). Crypto keeps its own wider stop (crypto_stop_width_pct).",
             "Cut losses short — stop ≤ 7–8%, never beyond 10%", "EQUITY",
             8.0, "Max stop distance below entry (%)", 5.0, 12.0, 65),
            ("exit_earnings_hold_cushion_pct", "EXIT_DEFENSIVE",
             "Hold through earnings if cushion ≥ 10%",
             "Minervini does not blanket-exit before every earnings report — he holds through a print "
             "when the position already has a comfortable profit cushion, and only avoids initiating into "
             "earnings. When enabled and the open gain is at least this threshold, the position is held "
             "through earnings; below the cushion it is exited per exit_earnings_avoid.",
             "Hold through earnings only with a profit cushion", "EQUITY",
             10.0, "Min open gain % to hold through earnings", 5.0, 40.0, 64),
            ("exit_trail_giveback_pct", "EXIT_OFFENSIVE",
             "Trailing give-back after activation: 10%",
             "After the trailing stop activates (exit_profit_target_2), the position is held as long as "
             "it keeps making new highs and is only exited once price retraces this much from its peak "
             "since entry. A 10% give-back lets a runner breathe while still locking in the bulk of an "
             "extended move — the opposite of dumping the whole position at an arbitrary fixed target.",
             "Trail a winner; exit on meaningful give-back from the high", "BOTH",
             10.0, "Max give-back from peak (%)", 5.0, 25.0, 73),
        ]
        orgs_list = conn.execute(text("SELECT id FROM organizations")).fetchall()
        for (rid, cat, label, desc, ref, at, thr, tlabel, tmin, tmax, so) in new_rules:
            for oid in [None] + [o[0] for o in orgs_list]:
                if oid is None:
                    exists = conn.execute(text(
                        "SELECT 1 FROM rule_configs WHERE rule_id = :r AND organization_id IS NULL"
                    ), {"r": rid}).fetchone()
                else:
                    exists = conn.execute(text(
                        "SELECT 1 FROM rule_configs WHERE rule_id = :r AND organization_id = :o"
                    ), {"r": rid, "o": oid}).fetchone()
                if exists:
                    continue
                conn.execute(text("""
                    INSERT INTO rule_configs (rule_id, organization_id, category, label, description,
                        minervini_ref, enabled_globally, is_mandatory, asset_types,
                        threshold, threshold_label, threshold_min, threshold_max, sort_order, updated_by)
                    VALUES (:rid, :oid, :cat, :label, :desc, :ref, true, false, :at,
                        :thr, :tlabel, :tmin, :tmax, :so, 'migration_007')
                """), {"rid": rid, "oid": oid, "cat": cat, "label": label, "desc": desc, "ref": ref,
                       "at": at, "thr": thr, "tlabel": tlabel, "tmin": tmin, "tmax": tmax, "so": so})

        # 3. Re-point exit_profit_target_2 label/description toward trailing behaviour
        conn.execute(text("""
            UPDATE rule_configs
            SET label = 'Activate trailing stop at 40% profit',
                description = 'Once the open gain reaches this level the remaining position is no longer '
                              'hard-sold at a fixed number — a trailing give-back stop activates '
                              '(exit_trail_giveback_pct) so big winners can keep running. If the trailing '
                              'rule is disabled this falls back to a hard full exit at the target.',
                minervini_ref = 'Let your winners run — trail, don''t cap',
                threshold_min = 20
            WHERE rule_id = 'exit_profit_target_2';
        """))
        conn.commit()
        logger.info("Migration 007 complete.")

        # ── Seed per-org multi-market config keys ──────────────────────────────
        multi_market_configs = [
            ("active_exchanges", "ASX,CRYPTO_INDEPENDENTRESERVE", "STRING",
             "Active Exchanges", "Comma-separated exchange keys: ASX,CRYPTO_INDEPENDENTRESERVE etc.", "trading", False),
            ("ibkr_account_usd", "", "STRING",
             "IBKR USD Account", "IBKR account for USD trades. Leave blank to use main account.", "ibkr", False),
            ("fx_audusd_override", "", "STRING",
             "AUD/USD Rate Override", "Manual FX override. Leave blank for live rate.", "trading", False),
            ("crypto_exchange_key", "CRYPTO_INDEPENDENTRESERVE", "STRING",
             "Crypto Exchange", "Active crypto exchange key, e.g. CRYPTO_INDEPENDENTRESERVE", "crypto", False),
            ("crypto_api_key", "", "STRING",
             "Crypto API Key", "API key for org's crypto exchange account.", "crypto", True),
            ("crypto_api_secret", "", "STRING",
             "Crypto API Secret", "API secret for org's crypto exchange account.", "crypto", True),
            ("crypto_testnet", "false", "BOOLEAN",
             "Crypto Testnet Mode", "Use exchange testnet for crypto orders.", "crypto", False),
            ("us_universe_scope",          "SP500+NASDAQ100", "STRING",
             "US Universe Scope", "Controls which US stocks are seeded for screening. SP500+NASDAQ100 = ~600 stocks (default). Run 'Refresh US Universe' after changing.", "trading", False),
            ("last_market_regime_ASX",    "UNKNOWN", "STRING", "ASX Market Regime",    "", "system", False),
            ("last_market_regime_NYSE",   "UNKNOWN", "STRING", "NYSE Market Regime",   "", "system", False),
            ("last_market_regime_NASDAQ", "UNKNOWN", "STRING", "NASDAQ Market Regime", "", "system", False),
            ("onboarding_completed", "true", "BOOLEAN", "Onboarding Completed", "Whether the organization has completed first-time setup", "general", False),
        ]
        orgs_res = conn.execute(text("SELECT id FROM organizations;")).fetchall()
        for org in orgs_res:
            org_id = org[0]
            for key, val, vtype, label, desc, group, is_secret in multi_market_configs:
                exists = conn.execute(text("""
                    SELECT 1 FROM system_configs WHERE key = :key AND organization_id = :org_id;
                """), {"key": key, "org_id": org_id}).fetchone()
                if not exists:
                    conn.execute(text("""
                        INSERT INTO system_configs (key, value, value_type, label, description, organization_id, "group", is_secret)
                        VALUES (:key, :val, :vtype, :label, :desc, :org_id, :group, :is_secret);
                    """), {"key": key, "val": val, "vtype": vtype, "label": label,
                           "desc": desc, "org_id": org_id, "group": group, "is_secret": is_secret})
        conn.commit()

        # ── entry_check_logs table ─────────────────────────────────────────────
        logger.info("Creating 'entry_check_logs' table...")
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS entry_check_logs (
                id              SERIAL PRIMARY KEY,
                organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                signal_id       INTEGER REFERENCES signals(id) ON DELETE SET NULL,
                ticker          VARCHAR(32) NOT NULL,
                exchange_key    VARCHAR(32),
                checked_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                price_current   NUMERIC(14,4),
                price_pivot     NUMERIC(14,4),
                price_stop      NUMERIC(14,4),
                price_vs_pivot  NUMERIC(8,4),
                vol_current     BIGINT,
                vol_avg_50      NUMERIC(20,2),
                vol_ratio       NUMERIC(8,4),
                ma_10           NUMERIC(14,4),
                ma_50           NUMERIC(14,4),
                ma_150          NUMERIC(14,4),
                ma_200          NUMERIC(14,4),
                high_52w        NUMERIC(14,4),
                low_52w         NUMERIC(14,4),
                pct_from_52w_high NUMERIC(8,4),
                rs_rating       NUMERIC(6,2),
                breakout_confirmed BOOLEAN DEFAULT FALSE,
                rule_results    JSONB DEFAULT '{}'::jsonb,
                data_source     VARCHAR(32),
                data_delay_mins INTEGER,
                bar_timestamp   TIMESTAMP WITHOUT TIME ZONE,
                bar_date        DATE
            );
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_ecl_org_checked
              ON entry_check_logs (organization_id, checked_at DESC);
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_ecl_ticker
              ON entry_check_logs (ticker);
        """))
        conn.commit()
        logger.info("entry_check_logs table ready.")

    # ── Migration 004 — MCP credential store ──────────────────────────────────
    logger.info("Running migration 004 — MCP credential store...")

    with engine.connect() as conn:
        def _table_exists_m4(tbl):
            return conn.execute(text(
                "SELECT 1 FROM information_schema.tables WHERE table_name=:t"
            ), {"t": tbl}).fetchone() is not None

        if not _table_exists_m4("mcp_credentials"):
            logger.info("Creating mcp_credentials table...")
            conn.execute(text("""
                CREATE TABLE mcp_credentials (
                    id                    SERIAL PRIMARY KEY,
                    organization_id       INTEGER NOT NULL
                                          REFERENCES organizations(id) ON DELETE CASCADE,
                    name                  VARCHAR(128) NOT NULL DEFAULT 'Default',
                    client_id             VARCHAR(64)  UNIQUE NOT NULL,
                    client_secret_hash    VARCHAR(256) NOT NULL,
                    client_secret_preview VARCHAR(16)  NOT NULL,
                    scopes                JSONB        NOT NULL DEFAULT '[]',
                    expires_at            TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                    is_active             BOOLEAN      NOT NULL DEFAULT TRUE,
                    created_at            TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW()),
                    created_by            VARCHAR(128),
                    last_used_at          TIMESTAMP WITHOUT TIME ZONE,
                    revoked_at            TIMESTAMP WITHOUT TIME ZONE,
                    revoked_by            VARCHAR(128),
                    notes                 TEXT
                );
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_mcp_cred_org ON mcp_credentials (organization_id);"
            ))
            conn.commit()
            logger.info("mcp_credentials table created.")

        # Ensure mcp_base_url is always global — remove any org-scoped rows that
        # may have been created by an earlier partial migration run
        try:
            conn.execute(text(
                "DELETE FROM system_configs WHERE key = 'mcp_base_url' AND organization_id IS NOT NULL;"
            ))
            conn.commit()
        except Exception:
            conn.rollback()

        # Seed global mcp_base_url SystemConfig (no org_id — super admin configurable)
        mcp_url_exists = conn.execute(text(
            "SELECT 1 FROM system_configs WHERE key = 'mcp_base_url' AND organization_id IS NULL;"
        )).fetchone()
        if not mcp_url_exists:
            logger.info("Seeding global system config 'mcp_base_url'...")
            conn.execute(text("""
                INSERT INTO system_configs
                    (key, value, value_type, label, description, "group", organization_id, is_secret)
                VALUES (
                    'mcp_base_url',
                    'https://vcpilot.astradigital.com.au',
                    'STRING',
                    'MCP Base URL',
                    'Public base URL used in MCP client config snippets. '
                    'Change this if hosted at a different domain.',
                    'mcp',
                    NULL,
                    FALSE
                ) ON CONFLICT DO NOTHING;
            """))
            conn.commit()
            logger.info("mcp_base_url seeded.")

    logger.info("SaaS/Multi-tenant migration and seeding complete!")


if __name__ == "__main__":
    migrate()
