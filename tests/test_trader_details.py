import pytest
import asyncio
from unittest.mock import MagicMock
from dashboard.main import _trader_data_inner
from app.models.signal import Signal, SignalStatus
from app.models.market import PriceBar, EntryCheckLog
from datetime import date, datetime

def test_trader_data_entry_details(db_session, org_and_account):
    org, account = org_and_account
    
    # 1. Create a Signal
    sig = Signal(
        ticker="APT.AX",
        exchange_key="ASX",
        asset_type="EQUITY",
        currency="AUD",
        signal_date=date.today(),
        organization_id=org.id,
        status=SignalStatus.PENDING,
        pivot_price=100.0,
        stop_price=90.0,
        target_price_1=120.0,
        target_price_2=140.0,
        rs_rating=85.0
    )
    db_session.add(sig)
    
    # 2. Create a PriceBar with MA info to test fallback logic
    bar = PriceBar(
        ticker="APT.AX",
        exchange_key="ASX",
        date=date.today(),
        open=95.0,
        high=98.0,
        low=94.0,
        close=97.0,
        volume=100000,
        ma_50=92.5,
        ma_200=88.0,
        rs_rating=85.0
    )
    db_session.add(bar)
    db_session.commit()
    
    # 3. Call _trader_data_inner with request containing session
    mock_request = MagicMock()
    mock_request.session = {"organization_id": org.id, "authenticated": True}
    
    response = asyncio.run(_trader_data_inner(mock_request, db_session))
    assert response.status_code == 200
    
    import json
    data = json.loads(response.body)
    assert "signals" in data
    assert len(data["signals"]) == 1
    
    s_data = data["signals"][0]
    assert s_data["ticker"] == "APT.AX"
    assert s_data["next_check_at"] is not None
    assert s_data["next_check_at"] != "TBD"
    
    # Check that fallback metrics are present since there is no EntryCheckLog
    assert s_data["ma_50"] == 92.5
    assert s_data["ma_200"] == 88.0
    assert s_data["rs_rating"] == 85.0
    assert s_data["rule_results"] == {}
    
    # 4. Now create an EntryCheckLog and verify it overrides the fallback
    chk = EntryCheckLog(
        organization_id=org.id,
        signal_id=sig.id,
        ticker="APT.AX",
        exchange_key="ASX",
        checked_at=datetime.utcnow(),
        price_current=99.0,
        price_pivot=100.0,
        ma_50=93.0,
        ma_200=89.0,
        rs_rating=86.0,
        rule_results={"vcp_breakout_price": {"passed": False, "value": -1.0, "threshold": 10.0, "message": "Price is below pivot"}}
    )
    db_session.add(chk)
    db_session.commit()
    
    response2 = asyncio.run(_trader_data_inner(mock_request, db_session))
    data2 = json.loads(response2.body)
    s_data2 = data2["signals"][0]
    
    # Verify values come from EntryCheckLog (chk)
    assert s_data2["ma_50"] == 93.0
    assert s_data2["ma_200"] == 89.0
    assert s_data2["rs_rating"] == 86.0
    assert s_data2["rule_results"]["vcp_breakout_price"]["passed"] is False
