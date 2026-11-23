"""Provision a separate SELECT + audit-INSERT login for the restricted release.

Host-side utility: reads generated JSON-quoted runtime env, changes only that
database, and rewrites its DATABASE_URL. Run before starting/recreating web.
Credentials are passed to psql on stdin, never in process arguments/output.
"""
import argparse
import json
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--runtime-env", type=Path, required=True)
    args = parser.parse_args()
    env = dict((k, json.loads(v)) for k, v in (
        line.split("=", 1) for line in args.runtime_env.read_text().splitlines() if line
    ))
    url = urlsplit(env["DATABASE_URL"])
    if url.username != "astratrade" or url.path != "/astratrade":
        raise RuntimeError("Expected fresh AstraTrade owner connection; refusing retry/reset")
    password = secrets.token_urlsafe(32)
    sql = f"""
BEGIN;
CREATE ROLE astratrade_review LOGIN PASSWORD '{password}'
 NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
REVOKE ALL ON DATABASE astratrade FROM PUBLIC;
GRANT CONNECT ON DATABASE astratrade TO astratrade_review;
GRANT USAGE ON SCHEMA public TO astratrade_review;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO astratrade_review;
GRANT INSERT ON public.audit_logs TO astratrade_review;
GRANT USAGE, SELECT ON SEQUENCE public.audit_logs_id_seq TO astratrade_review;
COMMIT;
"""
    result = subprocess.run([
        "docker", "exec", "-i", args.container, "psql", "-X", "-U", "astratrade",
        "-d", "astratrade", "-v", "ON_ERROR_STOP=1",
    ], input=sql, text=True, capture_output=True)
    if result.returncode:
        # SQL errors could echo the password-bearing statement; never print it.
        raise RuntimeError("Role provisioning failed; inspect database state privately before retry")
    env["DATABASE_URL"] = urlunsplit((url.scheme, f"astratrade_review:{password}@{url.hostname}:{url.port}", url.path, "", ""))
    args.runtime_env.write_text("\n".join(k + "=" + json.dumps(v) for k, v in env.items()) + "\n")
    args.runtime_env.chmod(0o600)
    print("Dedicated review role provisioned; market/business changes denied at database level")


if __name__ == "__main__":
    main()
