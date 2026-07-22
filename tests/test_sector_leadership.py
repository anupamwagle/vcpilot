from datetime import date, timedelta


class _Engine:
    def __init__(self, enabled=True, threshold=20):
        self.enabled = enabled
        self._threshold = threshold

    def is_enabled(self, rule_id):
        return self.enabled and rule_id == "entry_sector_leadership"

    def threshold(self, _rule_id):
        return self._threshold


def test_sector_leadership_allows_only_top_ranked_sector():
    from app.screener.sector_leadership import evaluate_sector_leadership

    sectors = {
        "Technology": [24.0, 21.0, 19.0],
        "Financials": [9.0, 7.0, 8.0],
        "Energy": [-2.0, 0.0, 1.0],
        "Utilities": [-8.0, -6.0, -7.0],
        "Health Care": [5.0, 4.0, 3.0],
    }
    leader = evaluate_sector_leadership("Technology", sectors, _Engine())
    laggard = evaluate_sector_leadership("Energy", sectors, _Engine())

    assert leader.passed is True
    assert leader.value["rank"] == 1
    assert laggard.passed is False


def test_sector_leadership_fails_closed_when_data_is_not_actionable():
    from app.screener.sector_leadership import evaluate_sector_leadership

    result = evaluate_sector_leadership("Technology", {"Technology": [10, 11, 12]}, _Engine())
    assert result.passed is False
    assert "Insufficient" in result.message


def test_sector_return_loader_uses_only_sufficiently_broad_sector_history(db_session):
    from app.models.market import Stock, PriceBar
    from app.screener.sector_leadership import load_sector_returns, SECTOR_LOOKBACK_BARS

    start = date(2026, 1, 1)
    for symbol, sector in (("AAA", "Technology"), ("BBB", "Technology"), ("CCC", "Technology"), ("DDD", "Energy")):
        db_session.add(Stock(ticker=symbol, exchange_code=symbol, exchange_key="NYSE", sector=sector))
        for offset in range(SECTOR_LOOKBACK_BARS + 1):
            db_session.add(PriceBar(
                ticker=symbol, exchange_key="NYSE", date=start + timedelta(days=offset),
                close=100 + offset + (5 if sector == "Technology" else 0),
            ))
    db_session.commit()

    returns = load_sector_returns(db_session, "NYSE")
    assert set(returns) == {"Technology"}
    assert len(returns["Technology"]) == 3
