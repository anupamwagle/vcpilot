def test_readiness_probe_accepts_detected_paper_gateway(monkeypatch):
    import scripts.paper_gateway_readiness as readiness

    class _PaperBroker:
        def __init__(self, organization_id=None):
            self.organization_id = organization_id
            self.is_connected = True
            self.detected_paper_mode = True
            self.account = "DU123456"
            self.host = "ibkr"
            self.port = 4004
            self.last_error = ""

        def __enter__(self): return self
        def __exit__(self, *args): return None
        def get_open_orders(self): return [{"ibkr_order_id": 1}]
        def get_open_positions(self): return []

    monkeypatch.setattr(readiness, "IBKRBroker", _PaperBroker)
    ok, result = readiness.probe(7)
    assert ok is True
    assert result["account"] == "DU123456"
    assert result["open_order_count"] == 1


def test_readiness_probe_refuses_non_paper_gateway(monkeypatch):
    import scripts.paper_gateway_readiness as readiness

    class _LiveBroker:
        def __init__(self, organization_id=None):
            self.is_connected = True
            self.detected_paper_mode = False
            self.account = "U123456"
            self.host = "ibkr"
            self.port = 4003

        def __enter__(self): return self
        def __exit__(self, *args): return None
        def get_open_orders(self): return []
        def get_open_positions(self): return []

    monkeypatch.setattr(readiness, "IBKRBroker", _LiveBroker)
    ok, result = readiness.probe()
    assert ok is False
    assert "refused" in result["reason"].lower()
