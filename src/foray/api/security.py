"""HTTP middleware: security headers and a request-body size cap."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response

from foray.config import Settings

# Protomaps' static assets site hosts the vector basemap's glyphs + sprites - a few MB of
# font/icon data, not the tiles themselves (those are the self-hosted PMTiles archive at
# cfg.basemap_url). Self-hosting the glyph/sprite assets alongside the archive is a later step.
_PROTOMAPS_ASSETS = "https://protomaps.github.io"


def _origin(url: str) -> str:
    """The ``scheme://host[:port]`` of ``url``, or ``""`` if it has no host (relative / blank)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def _content_security_policy(basemap_url: str = "") -> str:
    """Public-facing app serving an HTML+JS frontend - locked to exactly what the frontend needs.

    Leaflet + MapLibre GL are bundled as 'self'. script-src/connect-src third-party origins are
    limited to Nominatim (geocode autocomplete) and, for the vector basemap, Protomaps' asset
    site plus the configured PMTiles host. style-src needs 'unsafe-inline' because the frontend
    sets ``style="..."`` attributes directly (legend swatches, score bars, phenology cells) -
    much lower risk than script injection, an accepted gap. worker-src / ``blob:`` in img-src
    are for MapLibre GL's web workers and canvas/sprite blobs. The selected destination's
    satellite fill is 'self' only - proxied through our own routes (sources/satellite.py).
    """
    connect = ["'self'", "https://nominatim.openstreetmap.org", _PROTOMAPS_ASSETS]
    img = [
        "'self'",
        "https://static.inaturalist.org",
        "https://inaturalist-open-data.s3.amazonaws.com",
        _PROTOMAPS_ASSETS,
        "data:",
        "blob:",
    ]
    basemap_origin = _origin(basemap_url)
    if basemap_origin and basemap_origin not in connect:
        connect.append(basemap_origin)
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "worker-src 'self' blob:; "
        f"img-src {' '.join(img)}; "
        f"connect-src {' '.join(connect)}; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )


# Issue #82: only route accepting a body is POST /api/location (LocationBody - a few KB
# realistic max). Cloudflare's free-plan edge cap is 100MB with no app-level backstop
# otherwise, so this rejects oversized bodies before they're read/parsed.
_MAX_BODY_BYTES = 32 * 1024


def is_https(request: Request) -> bool:
    # Cloudflare terminates TLS and proxies to the droplet over plain HTTP, setting
    # X-Forwarded-Proto to the client-facing scheme - trust that over the raw connection
    # scheme so this is accurate in prod. Falls back to the direct scheme for local dev
    # (no proxy in front), so behavior stays correct over plain http://localhost too.
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def client_ip(request: Request) -> str:
    # The origin firewall only accepts inbound 80/443 from Cloudflare's ranges, so
    # CF-Connecting-IP is safe to trust - but only after confirming it's actually an IP,
    # since a misconfigured proxy or local dev could hand us arbitrary header junk that
    # would otherwise let the rate-limit dict grow unbounded and bypass per-IP limiting.
    header = request.headers.get("cf-connecting-ip")
    if header:
        try:
            ipaddress.ip_address(header)
            return header
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


def install_middleware(app: FastAPI, cfg: Settings | None = None) -> None:
    """Register the body-size cap and the security-header pass, inner layer first.

    ``limit_body_size`` is registered before ``security_headers`` so it ends up the inner
    layer - a 413 from it still gets the security headers applied on the way back out.
    The CSP is built once here so a configured ``basemap_url`` host is whitelisted in
    ``connect-src``.
    """
    csp = _content_security_policy((cfg or Settings()).basemap_url)

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next: Any) -> Response:
        if request.method in ("POST", "PUT", "PATCH"):
            content_length = request.headers.get("content-length")
            if content_length is not None:
                if not content_length.isdigit() or int(content_length) > _MAX_BODY_BYTES:
                    return Response(status_code=413, content="request body too large")
            else:
                # No Content-Length (e.g. chunked transfer-encoding) - enforce the same
                # ceiling by counting bytes off the stream instead of trusting the header.
                # bytearray avoids the repeated copy that `bytes += chunk` does on every
                # chunk (Copilot review caught this).
                body = bytearray()
                async for chunk in request.stream():
                    body += chunk
                    if len(body) > _MAX_BODY_BYTES:
                        return Response(status_code=413, content="request body too large")
                request._body = bytes(body)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
        if is_https(request):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
