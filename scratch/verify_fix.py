
import sys
import os
# Add the project root to sys.path so we can import 'app'
sys.path.append(os.getcwd())

from app.data.fetcher import get_intraday_price

def test_source(ticker, asset_type):
    print(f"Testing {ticker} ({asset_type})...")
    result = get_intraday_price(ticker, asset_type=asset_type)
    print(f"  Source: {result.get('data_source')}")
    print(f"  Price:  {result.get('price')}")
    print(f"  OK:     {result.get('ok')}")
    print("-" * 30)

if __name__ == "__main__":
    # 1. ASX stock should NOT use IR, even if asset_type is missing (defaults to EQUITY)
    test_source("YAL.AX", "EQUITY")
    
    # 2. Crypto should use IR
    test_source("BTC-AUD", "CRYPTO")
    
    # 3. ASX stock incorrectly labeled as CRYPTO (this is what caused the bug)
    # Now it should FAIL to fetch from IR because it doesn't match the IR code map, 
    # and fall back to others.
    test_source("YAL.AX", "CRYPTO")
