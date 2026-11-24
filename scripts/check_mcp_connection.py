"""Exercise real OAuth, MCP initialization, tool discovery/read and write denial."""
import json
import sys
from mcp_stdio_bridge import Bridge

bridge = Bridge(sys.argv[1])
def call(id, method, params):
    return bridge.request({"jsonrpc": "2.0", "id": id, "method": method, "params": params})

initialized = call(1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                    "clientInfo": {"name": "AstraTrade deployment verification", "version": "1"}})
assert "result" in initialized, "MCP initialization failed"
tools = call(2, "tools/list", {})
read = call(3, "tools/call", {"name": "get_positions", "arguments": {}})
assert not read["result"].get("isError"), "Read tool failed"
denied = call(4, "tools/call", {"name": "place_order", "arguments": {"signal_id": 2147483647}})
assert denied["result"].get("isError"), "Read-only credential unexpectedly allowed write tool"
print(json.dumps({"oauth": "passed", "initialize": "passed", "tool_count": len(tools["result"]["tools"]),
                  "read_tool": "passed", "write_scope_denied": "passed"}))
