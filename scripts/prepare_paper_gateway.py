"""Prepare a private, paper-only gateway environment from existing credentials.

Run explicitly; never prints credentials. Does not start any services.
"""
import os
import sys
from pathlib import Path
from dotenv import dotenv_values

source, destination = map(Path, sys.argv[1:3])
values = dotenv_values(source)
username, password = values.get("IBKR_USERNAME"), values.get("IBKR_PASSWORD")
if not username or not password:
    raise SystemExit("IBKR credentials are missing")
if any("\n" in value or "\r" in value for value in (username, password)):
    raise SystemExit("Multiline credentials are unsupported")
destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as output:
    output.write(f"TWS_USERID={username}\nTWS_PASSWORD={password}\n")
    output.write("TRADING_MODE=paper\nREAD_ONLY_API=yes\nTWOFA_TIMEOUT_ACTION=exit\n")
    output.write("RELOGIN_AFTER_TWOFA_TIMEOUT=no\nEXISTING_SESSION_DETECTED_ACTION=secondary\n")
    output.write("ALLOW_BLIND_TRADING=no\nTIME_ZONE=Australia/Sydney\nJAVA_HEAP_SIZE=384\n")
print("Private paper gateway configuration prepared (API read-only)")
