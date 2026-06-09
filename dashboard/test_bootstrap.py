import sys
import os

from app.database import SessionLocal
from app.models.account import Organization, Account, AccountTier
from app.models.config import SystemConfig
from app.models.signal import Watchlist, WatchlistLabel

def run_test():
    db = SessionLocal()
    try:
        print("Checking if test organization already exists...")
        existing = db.query(Organization).filter(Organization.name == "Bootstrap Test Org").first()
        if existing:
            print("Removing existing test organization...")
            db.delete(existing)
            db.commit()

        # Let's import the bootstrap function
        from dashboard.main import bootstrap_organization_data
        from app.models.config import ConfigValueType
        from app.models.account import OrganizationTier
        
        print("Creating test organization...")
        org = Organization(name="Bootstrap Test Org", tier=OrganizationTier.GOLD, is_active=True)
        db.add(org)
        db.flush()
        
        print(f"Created organization ID: {org.id}")
        
        # Seed account
        acc_tier = db.query(AccountTier).first()
        account = Account(
            name="Bootstrap Test Org Account",
            organization_id=org.id,
            tier_id=acc_tier.id,
            capital_aud=5000.0,
            is_active=True,
            is_paper=True
        )
        db.add(account)
        db.flush()

        # Seed system configs
        configs_to_seed = [
            ("onboarding_completed", "false", ConfigValueType.BOOLEAN, "Onboarding Completed", "Whether the organization has completed first-time setup"),
            ("whatsapp_session_name", f"org_{org.id}", ConfigValueType.STRING, "WhatsApp Session Name", "WAHA session name"),
            ("active_exchanges", "ASX,CRYPTO_INDEPENDENTRESERVE", ConfigValueType.STRING, "Active Exchanges", "Comma-separated exchange keys"),
        ]
        for key, val, vtype, label, desc in configs_to_seed:
            db.add(SystemConfig(
                key=key, value=val, value_type=vtype, label=label,
                description=desc, organization_id=org.id
            ))
        db.flush()

        print("Calling bootstrap_organization_data...")
        bootstrap_organization_data(db, org.id, active_exchanges="ASX,CRYPTO_INDEPENDENTRESERVE")
        
        # Verify
        labels = db.query(WatchlistLabel).filter(WatchlistLabel.organization_id == org.id).all()
        print(f"Seeded Watchlist Labels: {[l.name for l in labels]}")
        assert len(labels) == 8, f"Expected 8 labels, got {len(labels)}"
        assert "DeFi" in [l.name for l in labels], "Expected DeFi crypto label to be seeded"
        
        watchlist = db.query(Watchlist).filter(Watchlist.organization_id == org.id).all()
        print(f"Seeded Watchlist Items: {[w.ticker for w in watchlist]}")
        assert len(watchlist) == 11, f"Expected 11 items, got {len(watchlist)}"
        
        print("Test passed successfully!")
        
    finally:
        # Clean up
        print("Cleaning up...")
        org = db.query(Organization).filter(Organization.name == "Bootstrap Test Org").first()
        if org:
            db.delete(org)
            db.commit()
        db.close()

if __name__ == "__main__":
    run_test()
