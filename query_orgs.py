from app.database import SessionLocal
from app.models.account import Organization

db = SessionLocal()
orgs = db.query(Organization).all()
print("Orgs:")
for o in orgs:
    print(o.id, o.name, o.is_active)
