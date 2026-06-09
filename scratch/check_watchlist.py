from app.database import SessionLocal
from app.models.signal import Watchlist
from app.models.market import Stock
import json

db = SessionLocal()
items = db.query(Watchlist).all()
result = []
for i in items:
    s = db.query(Stock).filter(Stock.ticker == i.ticker).first()
    result.append({
        "ticker": i.ticker,
        "exchange_key": i.exchange_key,
        "asset_type": i.asset_type,
        "stock_asset_type": s.asset_type if s else "N/A"
    })
print(json.dumps(result, indent=2))
db.close()
