"""``/api/genera`` - the fungus-genus catalog and this device's selection (issue #79)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from psycopg_pool import ConnectionPool

from foray.api.deps import get_pool, resolve_device_id, set_device_cookie
from foray.api_models import GenusResult, StatusResponse
from foray.cache import add_genus as db_add_genus
from foray.cache import list_selected_genera as db_list_selected_genera
from foray.cache import remove_genus as db_remove_genus
from foray.cache import search_fungi_genera

router = APIRouter()


@router.get("/api/genera")
def get_genera(
    query: str = Query("", alias="q", max_length=200),
    pool: ConnectionPool = Depends(get_pool),
) -> list[GenusResult]:
    """Genus catalog search (issue #79) - empty query returns the most-observed genera."""
    with pool.connection() as conn:
        hits = search_fungi_genera(conn, query)
    return [GenusResult(**hit) for hit in hits]


@router.get("/api/genera/selected")
def get_selected_genera(
    request: Request,
    response: Response,
    pool: ConnectionPool = Depends(get_pool),
) -> list[GenusResult]:
    """This device's selected genera (issue #79 Phase 2) - empty means "everything nearby"."""
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    with pool.connection() as conn:
        hits = db_list_selected_genera(conn, device_id)
    return [GenusResult(**hit) for hit in hits]


@router.post("/api/genera/{taxon_id}")
def add_selected_genus(
    taxon_id: int,
    request: Request,
    response: Response,
    pool: ConnectionPool = Depends(get_pool),
) -> StatusResponse:
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    with pool.connection() as conn:
        db_add_genus(conn, device_id, taxon_id)
    return StatusResponse(status="added")


@router.delete("/api/genera/{taxon_id}")
def remove_selected_genus(
    taxon_id: int,
    request: Request,
    response: Response,
    pool: ConnectionPool = Depends(get_pool),
) -> StatusResponse:
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    with pool.connection() as conn:
        db_remove_genus(conn, device_id, taxon_id)
    return StatusResponse(status="removed")
