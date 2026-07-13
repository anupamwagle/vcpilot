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
            CREATE TABLE IF NOT EXISTS organization_memberships (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                role_id INTEGER REFERENCES roles(id) ON DELETE SET NULL,
                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW()),
                CONSTRAINT uq_user_org_membership UNIQUE (user_id, organization_id)
            );
            CREATE INDEX IF NOT EXISTS ix_org_memberships_user ON organization_memberships (user_id);
            CREATE INDEX IF NOT EXISTS ix_org_memberships_org ON organization_memberships (organization_id);
        """))
        conn.commit()

        # Multi-org backfill: give every existing single-org user a default membership
        # for their current home org so they immediately appear in the new model.
        conn.execute(text("""
            INSERT INTO organization_memberships (user_id, organization_id, is_default)
            SELECT u.id, u.organization_id, TRUE
            FROM users u
            WHERE u.organization_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM organization_memberships m
                  WHERE m.user_id = u.id AND m.organization_id = u.organization_id
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

        # Add precomputed VCP-geometry columns to watchlist (performance) if missing
        for _wl_col, _wl_type in [
            ("pivot_price", "NUMERIC(12, 4)"),
            ("stop_price", "NUMERIC(12, 4)"),
            ("target_price", "NUMERIC(12, 4)"),
            ("vcp_contractions", "INTEGER"),
            ("vcp_base_weeks", "INTEGER"),
            ("vcp_computed_date", "DATE"),
        ]:
            _exists = conn.execute(text(f"""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'watchlist' AND column_name = '{_wl_col}';
            """)).fetchone()
            if not _exists:
                logger.info(f"Adding '{_wl_col}' column to 'watchlist'...")
                conn.execute(text(f"ALTER TABLE watchlist ADD COLUMN {_wl_col} {_wl_type};"))
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
            ("telegram_enabled", "true", "BOOLEAN", "Telegram Alerts Enabled", "Enable or disable Telegram notifications", "notifications", False),
            ("telegram_bot_token", "", "STRING", "Telegram Bot Token", "The Telegram Bot Token from @BotFather", "notifications", True),
            ("telegram_chat_id", "", "STRING", "Telegram Chat ID(s)", "Comma-separated Telegram chat IDs to send alerts to — one per org user, or a single group chat ID", "notifications", False),
            ("ibkr_account", "", "STRING", "IBKR Account ID", "Interactive Brokers account number", "broker", False),
            ("novnc_url", "", "STRING", "Gateway Login URL (noVNC)", "noVNC URL for this org's IBKR Gateway. Managed via Super Admin → Org Detail.", "system", False),
            ("vnc_password", "", "STRING", "Gateway VNC Password", "VNC password for this org's IBKR Gateway. Managed via Super Admin → Org Detail.", "system", True),
            ("fmp_api_key", "", "STRING", "FMP API Key", "Financial Modeling Prep API key", "general", True),
            ("working_capital_aud", "5000.0", "FLOAT", "Working Capital (AUD)", "Working capital used for sizing and risk calculations", "general", False),
            ("working_capital_currency", "AUD", "STRING", "Working Capital Currency", "Currency of the working capital (e.g. AUD, USD, USDT, BNB)", "general", False),
            ("weekly_injection_aud", "0", "FLOAT", "Weekly Capital Injection (AUD)", "Amount of capital added to the account each week. Used for compounding position sizing calculations.", "risk", False),
            ("entry_limit_buffer_pct", "1.0", "FLOAT", "Entry Limit Buffer %", "How far above the stop trigger the limit sits on the automated BUY STOP-LIMIT breakout entry — caps slippage instead of chasing", "trading", False),
            ("trading_kill_switch", "false", "BOOLEAN", "Kill Switch", "Emergency halt: blocks ALL new entries immediately and cancels every working entry order. Flip via Telegram KILLSWITCH ON|OFF or here.", "trading", False),
            ("max_daily_loss_aud", "0", "FLOAT", "Max Daily Loss (AUD)", "Halt new entries for the day once today's realised+unrealised P&L breaches -this amount. 0 = disabled.", "trading", False),
            ("entry_skip_open_minutes", "10", "FLOAT", "Skip Entries After Open (min)", "Skip ASX entry checks for this many minutes after the 10:00am open — the staggered opening auction can confirm false breakouts on partial-day volume.", "trading", False),
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
                    actual_val = val
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

            # WhatsApp removed — drop legacy config rows left over from earlier sessions
            conn.execute(text("""
                DELETE FROM system_configs
                WHERE key IN ('whatsapp_enabled', 'whatsapp_admin_number', 'whatsapp_api_key',
                              'whatsapp_session_name', 'notification_channel')
                  AND organization_id = :org_id;
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

        # ── I2 (CLAUDE.md #41): report (don't auto-fix) duplicate ibkr_account
        # values across orgs. Two orgs sharing an account would both submit
        # orders to / reconcile against the same real account — a human must
        # decide which org keeps it, so this only logs, never deletes/edits.
        dupe_rows = conn.execute(text("""
            SELECT LOWER(TRIM(value)) AS acct, array_agg(organization_id) AS org_ids
            FROM system_configs
            WHERE key = 'ibkr_account' AND organization_id IS NOT NULL
              AND value IS NOT NULL AND TRIM(value) != ''
            GROUP BY LOWER(TRIM(value))
            HAVING COUNT(*) > 1;
        """)).fetchall()
        for acct, org_ids in dupe_rows:
            logger.warning(
                f"⚠️  Duplicate ibkr_account '{acct}' shared by organizations {list(org_ids)} — "
                f"orders/reconciliation for these orgs will collide against the same real IBKR "
                f"account. A human must pick which org keeps it (this migration does not auto-fix)."
            )

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

    # ── Migration 008 — user activity tracking columns on audit_logs ──────────
    logger.info("Running migration 008 — user activity tracking columns...")
    with engine.connect() as conn:
        def _col_exists_m8(tbl, col):
            return conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=:t AND column_name=:c"
            ), {"t": tbl, "c": col}).fetchone() is not None

        def _safe_m8(stmt):
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.debug(f"Migration 008 stmt skipped: {str(e)[:120]}")

        for col, defn in [
            ("feature",     "VARCHAR(64)"),
            ("http_method", "VARCHAR(8)"),
            ("ip_address",  "VARCHAR(45)"),   # belt-and-braces: model has it but older DBs may lack it
        ]:
            if not _col_exists_m8("audit_logs", col):
                logger.info(f"Adding audit_logs.{col}...")
                _safe_m8(f"ALTER TABLE audit_logs ADD COLUMN {col} {defn}")

        _safe_m8("CREATE INDEX IF NOT EXISTS ix_audit_logs_feature ON audit_logs (feature)")
        _safe_m8("CREATE INDEX IF NOT EXISTS ix_audit_logs_user_id ON audit_logs (user_id)")

        # Add the new AuditAction enum VALUES to the Postgres enum type. Adding
        # members to the Python enum does NOT alter the DB type, so without this
        # every activity-log INSERT fails with InvalidTextRepresentation.
        # ALTER TYPE ... ADD VALUE must run in AUTOCOMMIT (cannot be rolled back).
        try:
            ac_conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            for val in ("FEATURE_ACCESS", "FEATURE_ACTION"):
                try:
                    ac_conn.execute(text(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{val}'"))
                    logger.info(f"Ensured auditaction enum value '{val}'.")
                except Exception as e:
                    logger.debug(f"Migration 008 enum add skipped for {val}: {str(e)[:120]}")
        except Exception as e:
            logger.debug(f"Migration 008 enum block skipped: {str(e)[:120]}")

        logger.info("Migration 008 complete.")

    # ── Migration 009 — BROKER_SYNC exit reason ────────────────────────────────
    # sync_ibkr_positions_task used to tag orphan auto-closes as MANUAL, which the
    # UI explains as "closed manually by you" — misleading for an automated close.
    # Adding the Python enum member does NOT alter the Postgres enum type, so add
    # the value here (ALTER TYPE ... ADD VALUE must run in AUTOCOMMIT).
    logger.info("Running migration 009 — BROKER_SYNC exit reason enum value...")
    with engine.connect() as conn:
        try:
            ac_conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            ac_conn.execute(text("ALTER TYPE exitreason ADD VALUE IF NOT EXISTS 'BROKER_SYNC'"))
            logger.info("Ensured exitreason enum value 'BROKER_SYNC'.")
        except Exception as e:
            logger.debug(f"Migration 009 enum add skipped: {str(e)[:120]}")
        logger.info("Migration 009 complete.")

    # ── Migration 010 — orders.perm_id for fill reconciliation ─────────────────
    # sync_order_status matches live IBKR executions back to DB Order rows.
    # ibkr_order_id stores the client/session-scoped orderId; permId is IBKR's
    # globally-unique, reconnect-stable ID and is what executions carry, so it
    # needs its own column rather than overloading ibkr_order_id.
    logger.info("Running migration 010 — orders.perm_id column...")
    with engine.connect() as conn:
        def _col_exists_m10(tbl, col):
            return conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=:t AND column_name=:c"
            ), {"t": tbl, "c": col}).fetchone() is not None

        if not _col_exists_m10("orders", "perm_id"):
            try:
                conn.execute(text("ALTER TABLE orders ADD COLUMN perm_id INTEGER"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_orders_perm_id ON orders (perm_id)"))
                conn.commit()
                logger.info("Added orders.perm_id.")
            except Exception as e:
                conn.rollback()
                logger.debug(f"Migration 010 stmt skipped: {str(e)[:120]}")
        logger.info("Migration 010 complete.")

    # ── Migration 011 — R1: tighten risk_max_position_pct default 30% → 25% ────
    # Minervini's stated maximum in a single name is 20-25%; 30% as a *default*
    # over-concentrates every new org (50% is his cautionary example of
    # outright dangerous concentration, not a target the old minervini_ref text
    # implied). Only touches rows still at the untouched default 30 — never
    # clobbers a value an admin deliberately set.
    logger.info("Running migration 011 — tighten risk_max_position_pct default 30% -> 25%...")
    with engine.connect() as conn:
        before = conn.execute(text("""
            SELECT organization_id, threshold FROM rule_configs
            WHERE rule_id = 'risk_max_position_pct' AND threshold = 30;
        """)).fetchall()
        if before:
            logger.info(f"risk_max_position_pct at old default (30%) for orgs (NULL = global template): "
                        f"{[r[0] for r in before]}")
        result = conn.execute(text("""
            UPDATE rule_configs
            SET threshold = 25,
                label = 'Max position size: 25% of capital',
                description = 'No single position can exceed 25% of total capital.',
                minervini_ref = 'Concentration with control — 20-25% cap per name (50% is his '
                                'cautionary example of outright dangerous concentration, not a target)'
            WHERE rule_id = 'risk_max_position_pct' AND threshold = 30;
        """))
        conn.commit()
        logger.info(f"Migration 011: updated {result.rowcount} risk_max_position_pct row(s) 30% -> 25%.")
        # AW org (id=10) explicit check, per the audit's request to confirm the live org specifically.
        aw_row = conn.execute(text("""
            SELECT threshold FROM rule_configs
            WHERE rule_id = 'risk_max_position_pct' AND organization_id = 10;
        """)).fetchone()
        if aw_row is not None:
            logger.info(f"Migration 011: AW org (id=10) risk_max_position_pct is now {aw_row[0]}%.")
        logger.info("Migration 011 complete.")

    # ── Migration 012 — R3: failed-breakout defensive exit ──────────────────────
    # New Position.pivot_price column (carried from Signal.pivot_price at entry),
    # the FAILED_BREAKOUT exitreason enum value (ALTER TYPE must run in
    # AUTOCOMMIT — same pattern as Migration 009's BROKER_SYNC), and seeding the
    # exit_failed_breakout rule to the global template + every org (same
    # loop pattern as Migration 007's new_rules seeding).
    logger.info("Running migration 012 — failed-breakout defensive exit...")
    with engine.connect() as conn:
        if not conn.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'positions' AND column_name = 'pivot_price';
        """)).fetchone():
            conn.execute(text("ALTER TABLE positions ADD COLUMN pivot_price NUMERIC(14,4);"))
            conn.commit()
            logger.info("Added positions.pivot_price.")

        try:
            ac_conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            ac_conn.execute(text("ALTER TYPE exitreason ADD VALUE IF NOT EXISTS 'FAILED_BREAKOUT'"))
            logger.info("Ensured exitreason enum value 'FAILED_BREAKOUT'.")
        except Exception as e:
            logger.debug(f"Migration 012 enum add skipped: {str(e)[:120]}")

        orgs_list = conn.execute(text("SELECT id FROM organizations;")).fetchall()
        for oid in [None] + [o[0] for o in orgs_list]:
            if oid is None:
                exists = conn.execute(text(
                    "SELECT 1 FROM rule_configs WHERE rule_id = 'exit_failed_breakout' AND organization_id IS NULL"
                )).fetchone()
            else:
                exists = conn.execute(text(
                    "SELECT 1 FROM rule_configs WHERE rule_id = 'exit_failed_breakout' AND organization_id = :o"
                ), {"o": oid}).fetchone()
            if exists:
                continue
            conn.execute(text("""
                INSERT INTO rule_configs (rule_id, organization_id, category, label, description,
                    minervini_ref, enabled_globally, is_mandatory, threshold, threshold_label,
                    threshold_min, threshold_max, sort_order, updated_by)
                VALUES ('exit_failed_breakout', :oid, 'EXIT_DEFENSIVE',
                    '{EQUITY}',
		    'Exit failed breakout: close back below pivot within 3 days',
                    'A correct breakout should hold above the pivot almost immediately. If a daily '
                    'close falls back below the pivot buy point within this many days of entry, exit '
                    'rather than waiting for the full stop.',
                    'Cut a failed breakout fast — don''t wait for the full stop',
                    true, false, 3.0, 'Max days after entry to apply this check', 1.0, 10.0, 60.5,
                    'migration_012')
            """), {"oid": oid})
        conn.commit()
        logger.info("Migration 012 complete.")

    # ── Migration 013 — B13: LOGIN / LOGIN_FAILED auditaction enum values ──────
    # Adds the enum values only — no call site writes them yet (see the
    # AuditAction docstring in app/models/audit.py). Deliberately split from
    # switching login_post/login_verify_otp_post/logout over to them: this app
    # deploys Python changes via git-pull + uvicorn --reload with no gate, but
    # this migration only runs when the one-shot `migrate` service is
    # (re)started via `docker compose up`, not on every git pull — so writing
    # a not-yet-existent enum value from freshly-reloaded code would 500 every
    # login until someone manually reran the migration. Land this migration
    # first, confirm it has run in prod, then switch the call sites in a
    # separate follow-up change.
    logger.info("Running migration 013 — LOGIN/LOGIN_FAILED auditaction enum values...")
    with engine.connect() as conn:
        try:
            ac_conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            for val in ("LOGIN", "LOGIN_FAILED"):
                try:
                    ac_conn.execute(text(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{val}'"))
                    logger.info(f"Ensured auditaction enum value '{val}'.")
                except Exception as e:
                    logger.debug(f"Migration 013 enum add skipped for '{val}': {str(e)[:120]}")
        except Exception as e:
            logger.debug(f"Migration 013 enum block skipped: {str(e)[:120]}")
        logger.info("Migration 013 complete.")

    logger.info("SaaS/Multi-tenant migration and seeding complete!")


if __name__ == "__main__":
    migrate()
