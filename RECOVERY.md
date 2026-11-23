# AstraTrade migration checkpoint — 23 November 2026

Production deployment is blocked by source database corruption. Do not point DNS
at a replacement instance or initialise an empty database as a substitute.

Canonical project record: `D:/Dropbox/Home/Trading/03-Engineering/CODE-STATE.md`.
Existing source checkout: `C:/vcpilot` (WSL `/mnt/c/vcpilot`). Source base:
`31043d43e3e06b4071dc55990f8fc2023a3598a5`, matching GitHub and the NAS checkout.

## Verified local application

The isolated `astratrade-smoke` Compose project serves `http://localhost:8501`.
It uses newly seeded **synthetic test data**, not recovered production records.
Private local sign-in details are in `.runtime/local-review-login.txt`.

Restart on this machine:

```powershell
wsl -d Ubuntu -u root -- docker compose --env-file /home/dev_user/.local/state/astratrade-migration-20261123/smoke-compose.env -p astratrade-smoke -f /mnt/c/vcpilot/compose.recovery.yml up -d
```

Do not run the legacy `deploy.sh` for recovery. It starts workers and Beat and
runs migrations/seeding. `compose.recovery.yml` is standalone and has no workers,
gateway broker, scheduled jobs or automatic application migrations. Database and
web are on an internal Docker network; only nginx has a published loopback port.
Application egress to the NAS and the internet was tested and rejected. The web
process runs without root, with a read-only filesystem and dropped capabilities.

`ASTRA_RECOVERY_MODE=true` blocks mutations except password sign-in, and blocks
MCP, Telegram webhook, action and OAuth consent routes. Audit/access records can
still be written by normal sign-in and page views. This is a review environment,
not a forensic read-only database connection or an enabled trading environment.

The image retains the NAS dependency set rather than silently upgrading it:
`astratrade-nas-base:50155a1a25d5`, verified local image ID
`sha256:50155a1a25d5f1096997511c21dc7e6f4e1a38a6a57eb08857d634fde98e4b42`.
This was the NAS `vcpilot-web` image tag at export time; the old Created web
container referenced a different image. No production-readiness or dependency
security audit is implied by retaining these dependencies.

## Source data recovery

Private evidence is outside Git and Dropbox summaries in WSL:
`/home/dev_user/.local/state/astratrade-migration-20261123` (mode 0700).

- `nas-postgres-physical.tar.gz`: preserved source volume, 41,008,780 bytes.
  SHA256 `95fd98057dc75b957e299cee978d080dc018495c218c4b765a5cef3cd9ae1e03`.
- `nas-historical-20260904.tar.gz`: existing NAS recovery snapshot,
  199,443,949 bytes. SHA256
  `1a03a0e3c7b760713fac5dc4447050a2480c61c0b50e746b40828d78e291eba9`.
- Production environment file is preserved privately in the same directory.
  Never copy it into a build context, test runtime or ordinary report.

NAS database was briefly paused for a stable physical copy, then unpaused.
No source database files or business rows were repaired/changed. Its healthcheck
reports healthy even though SQL fails on `global/pg_filenode.map` permissions.
Data is owned by uid 999 while the image's PostgreSQL process uses uid 70.

Only disposable WSL copies were changed. Fixing ownership allowed startup but
revealed missing TimescaleDB metadata and missing `pg_attribute` entries affecting
orders, trades, signals, rule configuration and entry logs. Catalog reconstruction
was attempted transactionally and rolled back after a duplicate catalog-key error.
System reindexing stalled while checking a statistics tuple and was cancelled;
targeted reindexing then failed on missing transaction file `pg_subtrans/0005`.
The older snapshot failed to find a valid WAL checkpoint. A WAL reset **only on
its disposable forensic copy** allowed inspection and showed the same catalog
damage. Neither copy is an accepted restore. Both recovery containers are stopped;
their volumes and untouched archive files are retained.

No hypertables or chunks were reported by the readable catalog queries. Some
tables could be read, including historical price bars through 21 August 2026.
That does not establish completeness, correct financial balances or zero trades.

## Verification and next steps

Browser password sign-in/dashboard and seven authenticated HTTP pages/health
checks passed. Four action/MCP/webhook rejection checks and two egress checks
passed. The 15 new recovery tests pass. Full regression results and baseline
comparison are recorded in the canonical project checkpoint.

Obtain a known-good backup/NAS snapshot, or complete specialist recovery with
row/schema/constraint reconciliation and a successful logical dump/restore.
Keep all recovery evidence. Address inherited regression failures before trading
acceptance. Then rehearse restoration and migrations locally, verify all table
counts/content hashes and credentials, and deploy on the verified shared server
at `/opt/docker/astratrade` with a separate project and resource limits. That
directory has not been created. Recheck server capacity and Ledgr readiness.
Only after origin acceptance should the user update Cloudflare. No DNS or tunnel
settings have been changed, and no broker or trading worker was started.
