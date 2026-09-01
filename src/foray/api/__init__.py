"""FastAPI app: JSON API over the scoring engine + the server-rendered web UI.

This was a single ``api.py`` module built around one 850-line ``create_app`` closure; issue
#242 split it into this package (``app`` = the factory, ``routes/`` = one router per domain,
``deps`` = shared request helpers, ``state``/``security``/``refresh_runner`` = the plumbing).

``geocode``/``inat``/``scoring`` are re-exported here so ``foray.api.<mod>.<fn>`` stays a
valid ``monkeypatch.setattr`` target - they are the same module objects the route modules
import, so patching one patches the other.
"""

from __future__ import annotations

from foray import geocode, inat, scoring
from foray.api.app import create_app

__all__ = ["create_app", "geocode", "inat", "scoring"]
