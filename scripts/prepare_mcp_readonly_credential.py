"""Create private provisioning SQL and a read-only MCP bridge config.

The SQL must be applied explicitly by the database owner. This does not grant
trading permissions. Usage: script.py ORG_ID BASE_URL OUTPUT_DIRECTORY
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

org_id = int(sys.argv[1])
base_url = sys.argv[2]
out = Path(sys.argv[3])
out.mkdir(parents=True, exist_ok=True, mode=0o700)
client_id = "vcpilot_" + uuid.uuid4().hex[:20]
secret = "vcp_sk_" + os.urandom(32).hex()
salt = os.urandom(16)
hashed = salt.hex() + ":" + hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, 100000).hex()
scopes = ["trading:read", "signals:read", "watchlist:read", "rules:read", "config:read", "market:read"]
config = {"base_url": base_url, "client_id": client_id, "client_secret": secret}
sql = f"""BEGIN;
INSERT INTO mcp_credentials (organization_id,name,client_id,client_secret_hash,client_secret_preview,scopes,expires_at,is_active,created_at,notes)
SELECT {org_id},'Codex read-only verification','{client_id}','{hashed}','{secret[-8:]}','{json.dumps(scopes)}',NOW()+INTERVAL '90 days',true,NOW(),'Paper execution pending; read scopes only'
WHERE EXISTS (SELECT 1 FROM organizations WHERE id={org_id} AND is_active=true);
COMMIT;
"""
for name, data in (("client.json", json.dumps(config)), ("provision.sql", sql)):
    fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
print("Private MCP credential and owner provisioning SQL prepared (read-only scopes)")
