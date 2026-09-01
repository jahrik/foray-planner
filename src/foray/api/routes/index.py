"""``/`` - hand back the built SPA entry point (it fetches ``/api/config`` on load)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

from foray.api.paths import DIST

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index() -> Any:
    # The SPA fetches /api/config on load, so no server-side templating is needed -
    # just hand back the built entry point.
    if (DIST / "index.html").is_file():
        return FileResponse(DIST / "index.html")
    return HTMLResponse(
        "<h1>Foray Planner</h1><p>Frontend not built. Run "
        "<code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</code>.</p>",
        status_code=503,
    )
