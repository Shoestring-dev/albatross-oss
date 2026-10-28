#!/usr/bin/env python3
"""Push one Telegram group call to Albatross (see README.md in this folder). Standard library only.

    ALBATROSS_URL=http://localhost:8787 CALL_FEED_TOKEN=... python3 example_push.py <chain> <address> "<group>" "<caller>"
"""

import json
import os
import sys
import time
import urllib.request

url = os.environ.get("ALBATROSS_URL", "http://localhost:8787").rstrip("/") + "/api/ingest/calls"
token = os.environ["CALL_FEED_TOKEN"]
chain, address, group, caller = sys.argv[1:5]
now = time.time()
body = {"source": "example", "calls": [{"src_id": int(now * 1000), "chain": chain, "address": address,
                                         "group_name": group, "caller": caller, "intent": "buy", "called_at": now}]}
req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                             headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=30) as r:
    print(r.status, r.read().decode())
