#!/usr/bin/env python3
"""Liveness check: GET /api/config, exit 0 if it returns 200.

No curl/wget in the slim runtime image, so this uses stdlib urllib instead.
"""

import sys
import urllib.request

sys.exit(0 if urllib.request.urlopen("http://127.0.0.1:8000/api/config", timeout=4).status == 200 else 1)
