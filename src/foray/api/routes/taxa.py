"""``/api/taxa`` - search the taxon catalog (any rank) and this device's picked targets
(issues #79, #464)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from psycopg_pool import ConnectionPool

from foray.api.deps import get_pool, resolve_device_id, set_device_cookie
from foray.api_models import StatusResponse, TaxonRank, TaxonResult
from foray.cache import add_target, list_selected_targets, remove_target, search_taxa
from foray.taxa import SEARCHABLE_RANKS

router = APIRouter()


@router.get("/api/taxa/search")
def search_taxa_route(
    query: str = Query("", alias="q", max_length=200),
    rank: TaxonRank | None = None,
    pool: ConnectionPool = Depends(get_pool),
) -> list[TaxonResult]:
    """Taxon search by scientific name, common name or synonym, optionally within one rank (species
    to kingdom). An empty query returns the most-observed taxa of ``rank`` (genera by default)."""
    with pool.connection() as conn:
        hits = search_taxa(conn, query, rank=rank)
    return [TaxonResult(**hit) for hit in hits]


@router.get("/api/taxa/selected")
def get_selected_taxa(
    request: Request,
    response: Response,
    pool: ConnectionPool = Depends(get_pool),
) -> list[TaxonResult]:
    """This device's picked targets - empty means "everything nearby"."""
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    with pool.connection() as conn:
        hits = list_selected_targets(conn, device_id)
    return [TaxonResult(**hit) for hit in hits]


@router.post("/api/taxa/{taxon_id}")
def add_selected_taxon(
    taxon_id: int,
    request: Request,
    response: Response,
    pool: ConnectionPool = Depends(get_pool),
) -> StatusResponse:
    """Add a taxon to this device's target list (idempotent). 404 for an id the catalog does not know
    or whose rank is not searchable (species to kingdom - a section or variety is not a target), so a
    typo cannot silently narrow every ranking to nothing."""
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    with pool.connection() as conn:
        known = conn.execute(
            "SELECT 1 FROM taxa WHERE taxon_id = %s AND is_active AND rank = ANY(%s)",
            [taxon_id, list(SEARCHABLE_RANKS)],
        ).fetchone()
        if known is None:
            raise HTTPException(404, f"unknown taxon: {taxon_id}")
        add_target(conn, device_id, taxon_id)
    return StatusResponse(status="added")


@router.delete("/api/taxa/{taxon_id}")
def remove_selected_taxon(
    taxon_id: int,
    request: Request,
    response: Response,
    pool: ConnectionPool = Depends(get_pool),
) -> StatusResponse:
    """Remove a taxon from this device's target list (idempotent)."""
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    with pool.connection() as conn:
        remove_target(conn, device_id, taxon_id)
    return StatusResponse(status="removed")
