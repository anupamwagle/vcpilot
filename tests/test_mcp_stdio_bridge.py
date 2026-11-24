import importlib.util
from pathlib import Path
import json
import os

import pytest

spec = importlib.util.spec_from_file_location("mcp_bridge", Path(__file__).parents[1] / "scripts/mcp_stdio_bridge.py")
bridge_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_module)


def test_bridge_requires_https_and_private_secret_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"base_url": "http://public.example", "client_id": "test", "client_secret": "test"}))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="HTTPS"):
        bridge_module.Bridge(path)
    path.write_text(json.dumps({"base_url": "https://trade.astradigital.com.au", "client_id": "test", "client_secret": "test"}))
    path.chmod(0o644)
    if os.name != "nt":
        with pytest.raises(ValueError, match="private"):
            bridge_module.Bridge(path)


def test_bridge_does_not_retry_unknown_order_result(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"base_url": "https://trade.astradigital.com.au", "client_id": "test", "client_secret": "test"}))
    path.chmod(0o600)
    bridge = bridge_module.Bridge(path)
    monkeypatch.setattr(bridge, "access_token", lambda: "private-token")
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise TimeoutError()
    monkeypatch.setattr(bridge_module, "urlopen", fail)
    with pytest.raises(TimeoutError):
        bridge.request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "place_order"}})
    assert len(calls) == 1
