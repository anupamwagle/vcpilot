from app.database import SessionLocal
from app.models.signal import Signal, SignalStatus
from app.models.account import Organization

db = SessionLocal()
org = db.query(Organization).first()

pending_signals = db.query(Signal).filter(
    Signal.organization_id == org.id,
    Signal.status == SignalStatus.PENDING,
    Signal.asset_type == "CRYPTO"
).all()
print(f"Number of pending signals: {len(pending_signals)}")
for s in pending_signals:
    print(s.ticker, s.status, s.asset_type, s.organization_id)
