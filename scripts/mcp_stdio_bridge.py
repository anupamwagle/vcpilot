"""Standard-library stdio bridge to AstraTrade's authenticated HTTP MCP.

Client credentials stay in an owner-only file. Access tokens refresh without
putting secrets in Codex config or process arguments. Mutations are never retried
after transport failures. The server remains the authority for scopes.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


class Bridge:
    def __init__(self, config):
        path = Path(config)
        if path.is_symlink() or (os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077):
            raise ValueError("MCP credential file must be private (mode 0600), not a symlink")
        self.config = json.loads(path.read_text())
        self.base = self.config["base_url"].rstrip("/")
        parsed = urlparse(self.base)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}):
            raise ValueError("MCP requires HTTPS except on loopback")
        self.token, self.expires = None, 0

    def access_token(self):
        if self.token and time.monotonic() < self.expires:
            return self.token
        body = urlencode({"grant_type": "client_credentials", "client_id": self.config["client_id"],
                          "client_secret": self.config["client_secret"]}).encode()
        req = Request(self.base + "/mcp/oauth/token", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "User-Agent": "AstraTrade-MCP/1.0"})
        with urlopen(req, timeout=30) as response:
            token = json.load(response)
        self.token = token["access_token"]
        self.expires = time.monotonic() + max(0, int(token.get("expires_in", 3600)) - 60)
        return self.token

    def request(self, message):
        req = Request(self.base + "/mcp/", data=json.dumps(message).encode(), headers={
            "Authorization": "Bearer " + self.access_token(), "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-03-26",
            "User-Agent": "AstraTrade-MCP/1.0",
        })
        with urlopen(req, timeout=90) as response:
            data = response.read()
        return json.loads(data) if data else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    bridge = Bridge(args.config)
    for line in sys.stdin:
        message = None
        try:
            message = json.loads(line)
            response = bridge.request(message)
            if response is not None and "id" in message:
                print(json.dumps(response), flush=True)
        except Exception as exc:
            # Never expose response bodies, tokens or config in error output.
            detail = f"AstraTrade request failed ({type(exc).__name__}); outcome may be unknown. Do not retry an order before reconciliation."
            if isinstance(exc, HTTPError):
                detail = f"AstraTrade rejected the request (HTTP {exc.code})."
                if exc.code == 401:
                    bridge.token = None
            if isinstance(message, dict) and "id" in message:
                print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": detail}}), flush=True)
            else:
                print(detail, file=sys.stderr)


if __name__ == "__main__":
    main()
