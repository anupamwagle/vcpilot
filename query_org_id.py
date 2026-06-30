from app.database import SessionLocal
from app.models.signal import Signal

db = SessionLocal()
s = db.query(Signal).filter(Signal.ticker=="STRK-AUD").first()
if s:
    print(f"Org ID: {s.organization_id}")
    print(f"Exchange Key: {s.exchange_key}")
else:
    print("Not found")
