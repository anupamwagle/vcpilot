from app.database import SessionLocal
from app.models.signal import Signal

db = SessionLocal()
s = db.query(Signal).filter(Signal.ticker=="STRK-AUD").first()
if s:
    print(f"Asset Type: {s.asset_type}")
    print(f"Exchange Key: {s.exchange_key}")
else:
    print("Not found")
