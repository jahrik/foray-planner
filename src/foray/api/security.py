"""HTTP middleware: security headers and a request-body size cap."""

from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import FastAPI, Request, Response

# Public-facing app serving an HTML+JS frontend - locked down to what the frontend actually
# needs (Leaflet bundled as 'self'; connect-src/script-src third-party origins limited to
# Nominatim; img-src additionally allows OSM tiles and iNaturalist's photo hosts) so an XSS bug
# can't exfiltrate to or load script from anywhere else. style-src needs 'unsafe-inline' because
# the frontend sets `style="..."` attributes directly (map legend swatches, score bars, phenology
# heatmap cells) - much lower risk than script injection, so that's an accepted gap. The selected
# destination's satellite fill (map.ts showSatelliteOverlay) is 'self' only - it's proxied and
# cached through our own /api/destinations/{region_id}/satellite/* routes (sources/satellite.py)
# rather than the browser hitting Esri directly, so no arcgisonline.com entry is needed here.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' https://*.tile.openstreetmap.org "
    "https://static.inaturalist.org https://inaturalist-open-data.s3.amazonaws.com data:; "
    "connect-src 'self' https://nominatim.openstreetmap.org; "
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


def install_middleware(app: FastAPI) -> None:
    """Register the body-size cap and the security-header pass, inner layer first.

    ``limit_body_size`` is registered before ``security_headers`` so it ends up the inner
    layer - a 413 from it still gets the security headers applied on the way back out.
    """

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
        response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
        if is_https(request):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
