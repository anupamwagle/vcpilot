"""
AstraTrade MCP (Model Context Protocol) Server

Exposes AstraTrade's trading and screening capabilities as MCP tools so that
AI agents (Claude, etc.) can automate trades, run screens, and read portfolio
state on behalf of an organisation.

Authentication: OAuth 2.0 client_credentials grant.
  1. Super admin generates client_id + client_secret via /superadmin/organizations/{id}
  2. Client calls POST /mcp/oauth/token → receives a short-lived JWT
  3. All MCP requests carry: Authorization: Bearer <jwt>

Transport: HTTP + SSE (MCP Streamable HTTP transport via FastMCP).
Mount point: /mcp (relative to app root)

Token endpoint: POST /mcp/oauth/token
MCP SSE:        GET  /mcp/sse
MCP Messages:   POST /mcp/messages
"""
