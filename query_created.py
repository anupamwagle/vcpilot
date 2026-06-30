from app.database import SessionLocal
from app.models.signal import Signal

db = SessionLocal()
s = db.query(Signal).filter(Signal.ticker=="STRK-AUD").first()
print(s.created_at)
