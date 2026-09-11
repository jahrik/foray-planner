#!/usr/bin/env python3
"""Liveness check: GET /healthz, exit 0 if it returns 200.

No DB round trip (see api/routes/health.py) - a Postgres blip shouldn't get this container
restarted; that's what /healthz/data is for. No curl/wget in the slim runtime image, so this
uses stdlib urllib instead.
"""

import sys
import urllib.request

sys.exit(0 if urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=4).status == 200 else 1)
