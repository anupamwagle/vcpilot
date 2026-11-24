# Fresh production deployment — 23 November 2026

24 November update: see [IBKR/MCP acceptance](IBKR-PAPER-ACCEPTANCE.md).
Read-only authenticated MCP is now permitted in the recovery stack. Trading,
workers, broker access and configuration writes remain disabled. The earlier
test-failure count below describes the original recovery release.

User authorised a fresh production database after the legacy NAS corruption was
confirmed, with best-effort stock-data recovery. This supersedes the requirement
to restore old accounts/trades before deployment. Originals remain preserved.

Target: existing Ledgr host `as-lenovo-01`, `/opt/docker/astratrade`.
Public hostname: `trade.astradigital.com.au`. User owns the Cloudflare change.
Run only `compose.recovery.yml` plus `compose.production.yml`, project `astratrade`.
Do not use the legacy Compose/deploy script; no workers or broker gateway here.

## Scope

Fresh organisation and defaults; no old users, holdings, orders, trades, signals,
watchlists, financial balances or provider credentials. The default migration user
is disabled with a random password. The seeded paper account is inactive, has no
broker, and is labelled unconfigured with zero allocated capital. This is not a
claim about real household holdings or balances. Trading/kill-switch flags pause
entries, and actual enforcement is no workers, internal application network and
restricted web routes. All changes except password sign-in remain paused in this
initial production release; email/OTP, agents and live price refresh are disabled.

`SESSION_HTTPS_ONLY=true` protects cookies at the public HTTPS endpoint. Hostnames
are restricted. Only nginx joins the existing `proxy_network`; web/database/Redis
remain internal. Resource ceilings total 736 MiB; no build runs on production.
Read-only application filesystem, non-root app and dropped capabilities retained.
The web connection uses `astratrade_review`, a non-superuser with SELECT plus
audit-log INSERT/sequence privileges only. `scripts/provision_review_role.py`
creates a separate random credential from the host before web startup. Orders,
configuration changes and schema writes are denied by PostgreSQL as well.

## Historical market recovery

Recovered from the current NAS physical archive on a disposable WSL copy, with
sequential scans (damaged indexes omitted rows). Original source archive SHA256:
`95fd98057dc75b957e299cee978d080dc018495c218c4b765a5cef3cd9ae1e03`.

- 2,655 instrument rows restored.
- 94,696 price rows exported; 81,924 pass structural checks, covering 2,467 tickers
  from 10 June to 21 August 2026. Quarantined: 9,540 exchange mismatches, 1,337 OHLC
  bounds failures, 375 nonpositive/nonfinite OHLC, 1,520 missing instruments.
- 371 fundamentals rows counted; 10 unreadable due to damaged TOAST storage.
  Of 361 exported, 337 accepted; 15 unsuccessful/invalid payloads and nine missing
  instruments excluded.

Checks include unique keys, dates, finite positive OHLC, OHLC bounds, nonnegative
volume, instrument/exchange linkage and valid successful JSON fundamentals.
All accepted columns and counts compared after logical import using canonical
content hashes. Provider accuracy/completeness and calculated indicators are not
independently verified. No price data is represented as current market quotes.
Raw exports, quarantine IDs, manifests and preserved archives remain private in
WSL `.local/state/astratrade-migration-20261123`; no datasets/secrets in Git.

## Initial bootstrap and release

`scripts.bootstrap_fresh_production` requires explicit
`ASTRA_FRESH_BOOTSTRAP=confirmed` and an empty public schema. Re-running against a
populated schema refuses before changes. It runs existing migrations then removes
usable default credentials and sample funding. Run before exposing the web app.
Market exports are imported separately into fresh tables, verified, then backed
up logically. Existing initialization emits legacy hypertable/enum warnings;
these tables remain ordinary PostgreSQL tables. No hypertable conversion claimed.

Production receives a tested image and a clean logical database dump. Code is a
Git release archive under `releases/<commit>` with `current` pointing to it;
runtime secrets are generated separately under `.runtime` (0700, files 0600).
Never copy the NAS environment or start the synthetic smoke database in production.

```sh
cd /opt/docker/astratrade/current
docker compose --env-file ../.env.deploy -p astratrade \
  -f compose.recovery.yml -f compose.production.yml up -d
```

Gateway origin is `http://astratrade-origin:80` from the existing Cloudflare
connector's `proxy_network`. A loopback port 8503 is available for host checks:
`curl -H 'Host: trade.astradigital.com.au' http://127.0.0.1:8503/healthz`.
In the existing tunnel, user adds the published application hostname
`trade.astradigital.com.au` mapped to `http://astratrade-origin:80` (HTTP).
DNS alone without a matching tunnel route is insufficient. No DNS/tunnel changes
are made by this deployment. Official reference:
[Cloudflare routing](https://developers.cloudflare.com/tunnel/concepts/routing/).
Cookie behaviour follows [Starlette documentation](https://www.starlette.io/middleware/).

## Validation, backup and rollback

Before release: 15 restriction tests pass; seven authenticated page/health checks,
Secure/HttpOnly cookies, hostname rejection, disabled mutations and denied network
egress pass. Database verifies no business history, active legacy users, funding
or active accounts; accepted data hashes match exports. Re-bootstrap refusal tested.
All application checks were repeated with the restricted database role. Privilege
checks confirm order INSERT/config UPDATE denied and audit INSERT allowed.
Earlier full suite: 1,159 passed, eight inherited failures, three skipped; all eight
reproduced on original main. No trading acceptance or dependency security audit.

Before and after release, check Ledgr readiness, host memory and AstraTrade health.
Retain clean logical dump and checksums under production `.runtime/backups`; create
a new `pg_dump --format=custom --no-owner --no-acl` before subsequent changes.
Initial rollback: stop only project `astratrade` using the above Compose files;
do not use `down -v`, delete volumes, stop shared networks or alter other products.
Leave originals and fresh production database for review. Subsequent release
rollback must account for schema changes; an image switch alone may be insufficient.

After user DNS change, verify public HTTPS sign-in and routing. Enabling data jobs,
notifications, broker access or trading needs scoped implementation/acceptance;
inherited regression failures remain recorded for that work.
