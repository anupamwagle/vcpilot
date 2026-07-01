"""
Standalone MCP server entrypoint — runs the MCP tool-calling surface
(`/mcp/sse`, `/mcp/messages`) as its own ASGI app, independent of the
dashboard's uvicorn process.

This is an ADDITIVE deployment option. By default AstraTrade still mounts
create_mcp_app() in-process inside dashboard/main.py, so nothing changes
unless you deliberately cut over. To actually route MCP tool traffic here
instead of through the dashboard:

  1. Run this as its own container (see docker-compose.yml's `mcp-server`
     service / docker/Dockerfile.mcp).
  2. Point your reverse proxy so `/mcp/sse` and `/mcp/messages` go to this
     container's port instead of the dashboard's, while `/mcp/oauth/token`
     and `/authorize` keep going to the dashboard (they need the dashboard's
     login session and are not served here).
  3. Remove the `app.mount("/mcp", create_mcp_app())` line from
     dashboard/main.py once traffic is confirmed flowing through here, to
     avoid running the tool-serving logic in two places at once.

Shares the same Postgres DB and APP_SECRET_KEY as the dashboard (both read
from the same .env), so Bearer JWTs minted by the dashboard's
/mcp/oauth/token endpoint verify correctly here with no RPC between the
two processes.
"""
from __future__ import annotations

from app.mcp.server import create_mcp_app

app = create_mcp_app()
