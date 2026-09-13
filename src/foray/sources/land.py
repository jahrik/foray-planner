"""Public-land ownership ingest from ArcGIS REST feature services.

For the free-camping question ("can I sleep near here for free"), the first thing to know is
*who manages the ground*. **BLM** and **USFS** are the two agencies most relevant to dispersed
camping, and **Tribal** land status matters for the same reason, so this module pulls those
ownership layers as GeoJSON and caches the polygons for the map. It reports ownership and links
the official source - nothing more; it makes no claim about whether camping is permitted
anywhere (see AGENTS.md).

Four authoritative ArcGIS layers, queried with an envelope around home:

* **BLM Surface Management Agency** - national ownership layer; filtered to the BLM-managed
  polygons (``ADMIN_AGENCY_CODE='BLM'``).
* **USFS Administrative Forest Boundaries** - national forest units (good ``FORESTNAME``).
* **Census TIGERweb AIANNH** - federal American Indian reservations (sovereign nation land, not
  a federal land-management agency, but the same "who manages the ground" question applies).
* **PAD-US Fee Managers** (USGS) - the national ownership standard, filling the gap the three
  sources above leave: state parks, NPS units, and other non-BLM/USFS/tribal land show no
  ownership label at all today (issue #335) - every trail inside a National or State Park,
  including all of Prairie Creek. Filtered to government-managed polygons (``Mang_Type`` in
  FED/STAT/LOC/DIST/JNT/TRIB - private/NGO land is excluded, both to cut the national dataset's
  size and because it isn't the "who manages the ground for camping" question this module
  answers) and kept running alongside BLM/USFS/tribal, not replacing them: issue #335 PR 2
  checked live (two test envelopes, a PostGIS area-intersection comparison) whether this layer
  could retire the direct BLM/USFS sources and found it can't - PAD-US's Fee Managers cut only
  covered 2.6%-30% of BLM's own Surface Management Agency layer's area (most BLM-administered
  land in the West isn't fee-simple BLM title, so it's outside a *fee managers* layer entirely)
  and 72%-88% of USFS's boundary layer. Dropping either would be a real coverage regression, not
  a redundancy cleanup, so all four sources stay.

Geometry is generalized server-side (``maxAllowableOffset``) so the cached polygons stay light
enough for the field map, and stored as GeoJSON text (see ``cache.public_land``); the ``geom``
GIST index (PostGIS Phase 1, issue #268) serves "land near here". This is **ownership only**:
it never asserts
that camping is legal, just shows the land and links the official source (see AGENTS.md).

No API key is needed. Like the campground ingest, a single source being unreachable is skipped
rather than aborting the whole refresh.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg

from foray.cache import connection, is_ingested, record_ingest, upsert_public_land
from foray.config import Settings, coverage_envelope
from foray.geo import bbox_around
from foray.sources.http import USER_AGENT
from foray.sources.ingest_base import run_area_ingest

logger = logging.getLogger(__name__)

_PAGE_SIZE = 1000
# Server-side geometry generalization, in degrees (~0.005° ≈ 500 m). Keeps national-forest
# MultiPolygons small enough to cache and render on a phone; ownership shading needs no more.
_SIMPLIFY_DEG = 0.005

# Bump when the SOURCES tuple changes in a way that needs a re-pull (e.g. adding PAD-US here,
# issue #335 PR 1, a Copilot review catch on PR #352): both the coverage-wide marker
# (``land:coverage:v{N}``) and the home-radius prefix (``land:v{N}:``) below fold this in, so
# an existing deployment that already recorded the unversioned markers re-fetches once instead
# of silently keeping the new source's data missing forever.
_LAND_SOURCES_VERSION = 2


@dataclass(frozen=True)
class LandSource:
    """One ArcGIS ownership layer and how to read a unit name / id out of its features."""

    key: str  # short cache source tag, e.g. "blm"
    agency: str  # display agency, e.g. "BLM"
    query_url: str  # ArcGIS layer `.../query` endpoint
    where: str  # server-side filter (e.g. restrict to one agency)
    name_field: str  # property holding the unit name (matched case-insensitively)
    fallback_name: str  # used when the name property is absent/blank
    id_field: str = "OBJECTID"  # stable per-feature id (matched case-insensitively)
    extra_fields: tuple[str, ...] = ()  # additional outFields a custom id/agency needs
    # PAD-US's OBJECTID isn't stable across releases (issue #335) - a source that needs a
    # feature id built from other properties (plus the geometry bounds, to disambiguate
    # features that share a generic name) sets this instead of relying on id_field.
    make_id: Callable[[dict[str, Any], tuple[float, float, float, float]], str] | None = None
    # PAD-US's agency is a coded value (e.g. "SPR"), not a display name - a source that needs
    # to resolve one sets this instead of using the static `agency` field.
    agency_of: Callable[[dict[str, Any]], str] | None = None

    @property
    def source_url(self) -> str:
        """Human-facing link to the source service (drop the `/query` verb)."""
        return self.query_url.removesuffix("/query")

    @property
    def out_fields(self) -> str:
        """ArcGIS `outFields` value - id/name plus whatever a custom id/agency needs."""
        fields = dict.fromkeys((self.id_field, self.name_field, *self.extra_fields))
        return ",".join(fields)


# PAD-US's Manager Name is a coded value (e.g. "SPR", "NPS"), not a display name - this is
# its published coded-value domain (PAD-US Data Manual Table 5 / the "Agency Name" domain on
# the Fee_Managers_PADUS layer, confirmed live 2026-09-12).
_PADUS_AGENCY_NAMES: dict[str, str] = {
    "TVA": "Tennessee Valley Authority",
    "BLM": "Bureau of Land Management",
    "BOEM": "Bureau of Ocean Energy Management",
    "USBR": "Bureau of Reclamation",
    "FWS": "U.S. Fish and Wildlife Service",
    "USFS": "Forest Service",
    "DOD": "Department of Defense",
    "USACE": "Army Corps of Engineers",
    "DOE": "Department of Energy",
    "NPS": "National Park Service",
    "NRCS": "Natural Resources Conservation Service",
    "ARS": "Agricultural Research Service",
    "BIA": "Bureau of Indian Affairs",
    "NOAA": "National Oceanic and Atmospheric Administration",
    "BPA": "Bonneville Power Administration",
    "OTHF": "Other or Unknown Federal Land",
    "TRIB": "American Indian Lands",
    "SPR": "State Park and Recreation",
    "SDC": "State Department of Conservation",
    "SLB": "State Land Board",
    "SFW": "State Fish and Wildlife",
    "SDNR": "State Department of Natural Resources",
    "SDOL": "State Department of Land",
    "OTHS": "Other or Unknown State Land",
    "REG": "Regional Agency Land",
    "RWD": "Regional Water Districts",
    "CITY": "City Land",
    "CNTY": "County Land",
    "UNKL": "Other or Unknown Local Government",
    "NGO": "Non-Governmental Organization",
    "PVT": "Private",
    "JNT": "Joint",
    "OTHR": "Other",
    "UNK": "Unknown",
    "VI": "U.S. Virgin Islands Government",
    "AS": "American Samoa Government",
    "GU": "Guam Government",
    "MP": "Mariana Islands Government",
    "PR": "Puerto Rico Government",
    "FM": "Federated States of Micronesia Government",
    "MH": "Marshall Islands Government",
    "PW": "Palau Government",
    "UM": "U.S. Minor Outlying Islands Government",
    "DESG": "Designation",
}


def _padus_agency(props: dict[str, Any]) -> str:
    """Resolve PAD-US's coded `Mang_Name` to a display agency name."""
    code = _get(props, "Mang_Name")
    if not code:
        return "PAD-US"
    code = str(code).strip()
    return _PADUS_AGENCY_NAMES.get(code, code)


def _padus_id(props: dict[str, Any], bounds: tuple[float, float, float, float]) -> str:
    """A feature id independent of `OBJECTID`, which isn't stable across PAD-US releases.

    Unit name alone collides: many small unnamed parcels share the generic `Unit_Nm`
    "Unnamed site - Other State" (a real Copilot review catch, PR #352 - the earlier
    name-only hash mapped every one of those onto the same cache row, silently dropping
    all but the last). Folding in the geometry's centroid (rounded to ~100 m, coarser than
    server-side simplification jitter between releases) disambiguates distinct polygons
    while keeping the same real-world feature's id stable release to release.
    """
    code = str(_get(props, "Mang_Name") or "unk").strip().lower()
    unit = str(_get(props, "Unit_Nm") or "").strip().lower()
    min_lng, min_lat, max_lng, max_lat = bounds
    centroid = f"{(min_lat + max_lat) / 2:.3f}:{(min_lng + max_lng) / 2:.3f}"
    digest = hashlib.sha1(f"{unit}:{centroid}".encode()).hexdigest()[:10]
    return f"{code}:{digest}"


# Four ownership layers - BLM/USFS/tribal cover the agencies dispersed camping cares about
# most; PAD-US backstops everything else (state parks, NPS, ...). Endpoints confirmed against
# the live services; if one moves, only these constants change.
SOURCES: tuple[LandSource, ...] = (
    LandSource(
        key="blm",
        agency="BLM",
        query_url=("https://gis.blm.gov/arcgis/rest/services/lands/BLM_Natl_SMA_LimitedScale/MapServer/1/query"),
        where="ADMIN_AGENCY_CODE='BLM'",
        name_field="ADMIN_UNIT_NAME",
        fallback_name="BLM land",
    ),
    LandSource(
        key="usfs",
        agency="USFS",
        query_url=("https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_ForestSystemBoundaries_01/MapServer/0/query"),
        where="1=1",
        name_field="FORESTNAME",
        fallback_name="National Forest",
    ),
    # Census Bureau TIGERweb, layer 2 ("Federal American Indian Reservations") - sovereign
    # nation land, not a federal land-management agency like BLM/USFS, but the same "who
    # manages the ground" question applies. No auth, no rate-limit signup (issue #80).
    LandSource(
        key="tribal",
        agency="Tribal",
        query_url=("https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/AIANNHA/MapServer/2/query"),
        where="1=1",
        name_field="NAME",
        fallback_name="Tribal land",
    ),
    # PAD-US 4.1 Fee Managers (USGS, no key) - the national ownership backstop; see module
    # docstring. Confirmed live 2026-09-12: `Mang_Type` values seen include FED/STAT/NGO/TRIB.
    LandSource(
        key="padus",
        agency="PAD-US",
        query_url=(
            "https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/Fee_Managers_PADUS/FeatureServer/0/query"
        ),
        where="Mang_Type IN ('FED','STAT','LOC','DIST','JNT','TRIB')",
        name_field="Unit_Nm",
        fallback_name="Protected area",
        extra_fields=("Mang_Name",),
        make_id=_padus_id,
        agency_of=_padus_agency,
    ),
)


def _envelope(lat: float, lng: float, radius_km: float) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) lon/lat box enclosing the home disk - the ArcGIS query bbox."""
    bbox = bbox_around(lat, lng, radius_km)
    return (bbox.min_lng, bbox.min_lat, bbox.max_lng, bbox.max_lat)


def _get(props: dict[str, Any], field: str) -> Any:
    """Case-insensitive property lookup - ArcGIS geojson lowercases requested field names."""
    if field in props:
        return props[field]
    lowered = field.lower()
    for key, value in props.items():
        if key.lower() == lowered:
            return value
    return None


def _bounds(coordinates: Any) -> tuple[float, float, float, float] | None:
    """Bounding box (min_lng, min_lat, max_lng, max_lat) of arbitrarily nested GeoJSON coords."""
    min_lng = min_lat = float("inf")
    max_lng = max_lat = float("-inf")
    found = False
    stack = [coordinates]
    while stack:
        item = stack.pop()
        if (
            isinstance(item, (list, tuple))
            and len(item) >= 2
            and isinstance(item[0], (int, float))
            and isinstance(item[1], (int, float))
        ):
            lng, lat = float(item[0]), float(item[1])
            min_lng, max_lng = min(min_lng, lng), max(max_lng, lng)
            min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
            found = True
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return (min_lng, min_lat, max_lng, max_lat) if found else None


def _parse_feature(source: LandSource, feature: dict[str, Any]) -> tuple[Any, ...] | None:
    """One ArcGIS GeoJSON feature -> a public_land row tuple, or None if unusable."""
    geometry = feature.get("geometry")
    if not geometry or not geometry.get("coordinates"):
        return None
    props = feature.get("properties") or {}
    # A geometry whose coords contain no numeric pair is junk - drop it here rather than let
    # the geom trigger silently store NULL. (The bbox is no longer persisted since issue #268
    # PR 5; the `geom` GIST index serves "land near here".) Computed once and reused as the
    # `make_id` discriminator below rather than walking the coordinate tree twice.
    bounds = _bounds(geometry["coordinates"])
    if bounds is None:
        return None
    feature_id = source.make_id(props, bounds) if source.make_id else _get(props, source.id_field)
    if feature_id in (None, ""):
        return None
    name = _get(props, source.name_field)
    unit = str(name).strip() if name not in (None, "") else source.fallback_name
    agency = source.agency_of(props) if source.agency_of else source.agency
    return (
        f"{source.key}:{feature_id}",
        agency,
        unit,
        source.key,
        source.source_url,
        json.dumps(geometry, separators=(",", ":")),
    )


def _iter_features(
    client: httpx.Client, source: LandSource, envelope: tuple[float, float, float, float]
) -> Iterator[dict[str, Any]]:
    """Yield every feature ArcGIS returns for the envelope, paging until the transfer limit."""
    xmin, ymin, xmax, ymax = envelope
    offset = 0
    while True:
        resp = client.get(
            source.query_url,
            params={
                "f": "geojson",
                "where": source.where,
                "geometry": f"{xmin},{ymin},{xmax},{ymax}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "outSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": source.out_fields,
                "returnGeometry": "true",
                "maxAllowableOffset": _SIMPLIFY_DEG,
                "resultOffset": offset,
                "resultRecordCount": _PAGE_SIZE,
            },
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        payload = resp.json()
        features = payload.get("features", [])
        if not features:
            return
        yield from features
        offset += len(features)
        # ArcGIS flags a truncated page; without the flag, a short page means we're done.
        if not payload.get("exceededTransferLimit") or len(features) < _PAGE_SIZE:
            return


def _fetch_public_land_envelope(
    envelope: tuple[float, float, float, float],
    *,
    client: httpx.Client | None = None,
    sources: Iterable[LandSource] = SOURCES,
    progress_cb: Callable[[str, float], None] | None = None,
) -> list[tuple[Any, ...]]:
    """Fetch ownership polygons within a (xmin, ymin, xmax, ymax) envelope, deduped by id.

    A source that fails is skipped so the others still ingest - ownership is best-effort
    context, never a hard dependency of the refresh. "Fails" covers both transport errors
    (``httpx.HTTPError``) and a service returning something other than well-formed GeoJSON
    (a decode error - ``ValueError`` - or an unexpected shape - ``KeyError``/``TypeError``).
    """
    owns = client is None
    client = client or httpx.Client(timeout=60.0)
    by_id: dict[str, tuple[Any, ...]] = {}
    try:
        sources_list = list(sources)
        total = len(sources_list)
        for index, source in enumerate(sources_list):
            if progress_cb:
                progress_cb(
                    f"Fetching {source.agency} land…",
                    ((index + 1) / total) * 100.0 if total else 100.0,
                )
            before = len(by_id)
            try:
                for feature in _iter_features(client, source, envelope):
                    row = _parse_feature(source, feature)
                    if row is not None:
                        by_id[row[0]] = row
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                logger.warning("land: source %s failed (%s) - skipping", source.key, error)
                continue  # skip this source; keep whatever the others returned
            logger.info("land: %s returned %d units", source.key, len(by_id) - before)
    finally:
        if owns:
            client.close()
    return list(by_id.values())


def fetch_public_land(
    *,
    lat: float,
    lng: float,
    radius_km: float,
    client: httpx.Client | None = None,
    sources: Iterable[LandSource] = SOURCES,
    progress_cb: Callable[[str, float], None] | None = None,
) -> list[tuple[Any, ...]]:
    """Fetch ownership polygons near home from each source, deduped by id."""
    return _fetch_public_land_envelope(
        _envelope(lat, lng, radius_km), client=client, sources=sources, progress_cb=progress_cb
    )


def ingest_public_land(
    cfg: Settings,
    con: psycopg.Connection | None = None,
    *,
    client: httpx.Client | None = None,
    sources: Iterable[LandSource] = SOURCES,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Ingest public-land ownership polygons into the cache. Returns rows upserted."""
    return run_area_ingest(
        cfg,
        con,
        prefix=f"land:v{_LAND_SOURCES_VERSION}:",
        label="land",
        noun="Public land",
        fetch=lambda **kw: fetch_public_land(client=client, sources=sources, **kw),
        upsert=upsert_public_land,
        progress_cb=progress_cb,
    )


def ingest_public_land_coverage(
    cfg: Settings,
    con: psycopg.Connection | None = None,
    *,
    client: httpx.Client | None = None,
    sources: Iterable[LandSource] = SOURCES,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Ingest BLM/USFS/tribal/PAD-US ownership across all of ``cfg.coverage`` in one envelope
    query.

    A single request covers the whole union bbox - ArcGIS's own pagination (see
    ``_iter_features``) already handles arbitrarily many results, so there's no need to chunk
    by region the way ``trails.py`` has to for Overpass. One-shot per ``SOURCES`` version:
    skips once ``land:coverage:v{N}`` is in ``ingest_log``, same self-heal as trails/camps/
    dispersed - bumping ``_LAND_SOURCES_VERSION`` re-pulls every region on the next
    ``refresh --with land --all`` cron.
    """
    key = f"land:coverage:v{_LAND_SOURCES_VERSION}"
    with connection(con) as database:
        if is_ingested(database, key):
            logger.info("land: coverage-wide ownership already ingested at v%d, skipping", _LAND_SOURCES_VERSION)
            if progress_cb:
                progress_cb("Public land already cached, skipping…", 100.0)
            return 0
        envelope = coverage_envelope(cfg.coverage)
        logger.info("land: fetching ownership across %d coverage regions…", len(cfg.coverage))
        rows = _fetch_public_land_envelope(envelope, client=client, sources=sources, progress_cb=progress_cb)
        upsert_public_land(database, rows)
        record_ingest(database, key, len(rows))
        database.execute("DELETE FROM ingest_log WHERE key LIKE %s AND key <> %s", ["land:coverage:v%", key])
        logger.info("land: cached %d public-land units (coverage-wide)", len(rows))
        return len(rows)
