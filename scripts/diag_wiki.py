import pandas as pd
import requests
import sys

url = "https://en.wikipedia.org/wiki/S%26P/ASX_200"
headers = {"User-Agent": "Mozilla/5.0 (compatible; VCPilot/1.0)"}
try:
    resp = requests.get(url, headers=headers, timeout=15)
    print(f"Status: {resp.status_code}")
    tables = pd.read_html(resp.text)
    print(f"Tables found: {len(tables)}")
    for i, t in enumerate(tables[:8]):
        cols = list(t.columns)
        print(f"Table {i}: cols={cols}, rows={len(t)}")
        # Check for Code/Ticker column
        lower_cols = [str(c).lower() for c in cols]
        if any(x in lower_cols for x in ["code", "ticker"]):
            print(f"  *** MATCH! First 5 rows:")
            print(t.head(5).to_string())
except Exception as e:
    print(f"ERROR: {e}")
    import traceback; traceback.print_exc()
