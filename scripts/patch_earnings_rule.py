"""
One-off patch: update exit_earnings_avoid rule to 5-day buffer, 20-day max.
Run via: wsl bash -c "cd /mnt/c/vcpilot && docker compose exec app python scripts/patch_earnings_rule.py"
"""
import sys
sys.path.insert(0, "/app")

from app.database import SessionLocal
from app.models.config import RuleConfig

with SessionLocal() as db:
    rows = db.query(RuleConfig).filter(RuleConfig.rule_id == "exit_earnings_avoid").all()
    for r in rows:
        print(f"Before — org_id={r.organization_id}: threshold={r.threshold}, max={r.threshold_max}")
        r.threshold = 5.0
        r.threshold_max = 20.0
        r.description = (
            "Exit positions N trading days before the next earnings date. "
            "Early warning fires at 3× this buffer and surfaces in audit log without forcing exit."
        )
    db.commit()
    for r in rows:
        print(f"After  — org_id={r.organization_id}: threshold={r.threshold}, max={r.threshold_max}")
    print(f"Updated {len(rows)} rule_config rows.")
