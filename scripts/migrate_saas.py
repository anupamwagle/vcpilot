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
                      AND key NOT IN ('last_market_regime', 'last_regime_check', 'last_heartbeat');
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
        # Ensure all organizations have the required system config keys
        configs_to_ensure = [
            ("trading_paused", "false", "BOOLEAN", "Trading Paused", "Toggles automated trade placement", "general", False),
            ("whatsapp_enabled", "true", "BOOLEAN", "WhatsApp Alerts", "Enables real-time notifications", "whatsapp", False),
            ("whatsapp_admin_number", "", "STRING", "WhatsApp Admin Number", "Number to send alerts and receive commands JID format", "whatsapp", False),
            ("whatsapp_api_key", settings.waha_api_key, "STRING", "WhatsApp API Key", "API key for the WhatsApp (WAHA) service", "whatsapp", True),
            # Session name set per-org below using org_id — do not hardcode "default" here
            ("whatsapp_session_name", "default", "STRING", "WhatsApp Session Name", "WAHA session name (always 'default' for WAHA Core; use WAHA Plus for per-org sessions)", "whatsapp", False),
            ("ibkr_account", "", "STRING", "IBKR Account ID", "Interactive Brokers account number", "broker", False),
            ("ibkr_username", "", "STRING", "IBKR Username", "Interactive Brokers login username", "broker", False),
            ("ibkr_password", "", "STRING", "IBKR Password", "Interactive Brokers login password", "broker", True),
            ("ibkr_paper_mode", "true", "BOOLEAN", "IBKR Paper Mode", "Use paper trading environment", "broker", False),
            ("fmp_api_key", "", "STRING", "FMP API Key", "Financial Modeling Prep API key", "general", True),
            ("weekly_injection_aud", "1000.0", "FLOAT", "Weekly Injection (AUD)", "Capital added weekly for sizing", "general", False),
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
                    conn.execute(text("""
                        INSERT INTO system_configs (key, value, value_type, label, description, organization_id, "group", is_secret)
                        VALUES (:key, :value, :value_type, :label, :description, :org_id, :group, :is_secret);
                    """), {
                        "key": key, "value": val, "value_type": vtype, "label": label,
                        "description": desc, "org_id": org_id, "group": group, "is_secret": is_secret
                    })

            has_rules = conn.execute(text("SELECT 1 FROM rule_configs WHERE organization_id = :org_id LIMIT 1;"), {"org_id": org_id}).fetchone()
            if not has_rules:
                # Check if template rules exist
                has_templates = conn.execute(text("SELECT 1 FROM rule_configs WHERE organization_id IS NULL LIMIT 1;")).fetchone()
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

    logger.info("SaaS/Multi-tenant migration and seeding complete!")


if __name__ == "__main__":
    migrate()
