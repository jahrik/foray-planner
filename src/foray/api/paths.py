"""Filesystem locations of the built frontend bundle.

The client is a Vite/TypeScript app (see frontend/); ``npm run build`` emits its bundle
into ``web/dist``. It is absent only when the frontend has not been built (e.g. a fresh
checkout running the API directly) - ``/`` then shows a hint instead of 500-ing, so
``foray openapi`` still works.
"""

from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).parent.parent / "web"
DIST = WEB / "dist"
