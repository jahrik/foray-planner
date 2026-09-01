"""Route modules, one per domain, each exposing an ``APIRouter`` as ``router``.

They are registered in :func:`foray.api.app.create_app` in the order the paths should
appear in the OpenAPI schema (``foray openapi`` output is drift-checked, so the order is
load-bearing).
"""
