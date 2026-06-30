from app.database import SessionLocal
from app.models.signal import Signal

db = SessionLocal()
signals = db.query(Signal).all()
for s in signals:
    print(f"ID: {s.id}, Ticker: {s.ticker}, Status: {s.status.name}, Exchange: {s.exchange_key}")
