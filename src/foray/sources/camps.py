"""Developed-campground ingest from the Recreation.gov RIDB API.

RIDB (https://ridb.recreation.gov/) is the authoritative dataset for *developed*
campgrounds - named sites with facilities. It needs a free API key, read from the
``RIDB_API_KEY`` environment variable (never committed; a gitignored ``.env`` locally, a
container/systemd env var on the server). If the key is absent, camps ingest is skipped
so the iNaturalist refresh still works.

RIDB's point+radius facilities search silently under-returns - it only matches facilities
whose own ``FacilityLatitude/Longitude`` is populated and near the point, which measured at
~1/3 of what is actually there (45 camping facilities within 50 mi of Eugene, OR by state
listing vs 13 by radius search). So the primary path is a **per-state** listing
(``facilities?state=XX&activity=CAMPING``, paged), with each facility clipped to the true
home radius by ``haversine_km``. The state set is the ``cfg.coverage`` regions whose bbox
reaches the home disk. Homes outside the US (no state resolves) fall back to the old
radius-tiling path.

Dispersed (free, undeveloped) camping has no authoritative dataset and is a separate,
proxy-based layer (tracked separately) - this module only handles developed campgrounds.
"""

from __future__ import annotations

import html
import logging
import math
import os
import re
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx
import psycopg

from foray import cache
from foray.cache import upsert_campsites
from foray.config import CoverageRegion, Settings, coverage_envelope
from foray.geo import KM_PER_DEG_LAT, haversine_km
from foray.sources.http import SOURCE_ERRORS, USER_AGENT, Throttle, retry_after_seconds
from foray.sources.ingest_base import run_area_ingest

logger = logging.getLogger(__name__)

RIDB_FACILITIES = "https://ridb.recreation.gov/api/v1/facilities"

_KM_PER_MILE = 1.609344
# RIDB caps the facilities-search radius; 50 mi is the documented max. Query circles of this
# radius are tiled over the home disk (see `_query_centers`).
_QUERY_RADIUS_MI = 50.0
_PAGE_SIZE = 50  # RIDB's max page size for the facilities endpoint

# RIDB rate-limits at 50 requests/minute; a wide radius tiles into dozens of requests, so
# pace them to stay comfortably under (45/min) and back off on a 429.
_MIN_REQUEST_INTERVAL = 60.0 / 45.0

# The tiling grid grows O(radius^2); config allows radii up to 20000 km, which would
# otherwise queue hundreds of thousands of RIDB requests (issue #115). Cap the grid and keep
# the centers closest to home when a radius would exceed it, rather than growing unbounded.
_MAX_QUERY_CENTERS = 200

# Fee descriptions that explicitly signal no charge. We only ever *assert* free on one of
# these; anything else stays unknown (NULL) rather than guessing paid - see AGENTS.md.
_FREE_MARKERS = ("no fee", "no charge", "free of charge", "fee: none", "$0", "$0.00")

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
# Dollar amounts in a fee blob, e.g. "$16", "$8.00", "$ 20". Amounts over this are almost
# always a fine / deposit / annual-pass price, not a nightly campsite fee - ignore them.
_DOLLARS = re.compile(r"\$\s?(\d{1,3}(?:\.\d{2})?)")
_MAX_PLAUSIBLE_NIGHTLY_USD = 150.0
# An amount whose trailing context mentions one of these isn't the base per-night site fee -
# it's an add-on / discount / non-camping line. Checked against the ~30 chars after the "$N".
_NON_NIGHTLY_CONTEXT = re.compile(
    r"extra\s+(vehicle|car)|additional\s+(vehicle|car)|per\s+extra|day[\s-]?use|senior|golden\s+age"
    r"|access\s+pass|annual\s+pass|interagency|reservation\s+fee|booking\s+fee|deposit|cancellation"
    r"|dump\s+station|firewood|shower|pet\s+fee",
    re.IGNORECASE,
)


def _clean_text(text: str | None) -> str | None:
    """Strip HTML tags/entities and collapse whitespace - RIDB fee fields ship raw markup."""
    if not text:
        return None
    stripped = _WHITESPACE.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()
    return stripped or None


def _fee_range(fee: str | None) -> tuple[float | None, float | None]:
    """(low, high) per-night USD parsed from a cleaned fee blob, or (None, None).

    RIDB ships fees as prose ("Camping Fees are $16/vehicle... $2 per extra vehicle"), so this
    is best-effort: pull the dollar amounts, drop the ones whose trailing context marks them as
    an add-on / discount / non-camping line (extra vehicle, day use, senior, deposit, ...), and
    take the span of what's left. ``low == high`` when one price survives; both ``None`` when
    none do.
    """
    text = fee or ""

    def is_nightly(match: re.Match[str]) -> bool:
        # Context = the ~25 chars before the amount ("Senior discount $4") plus its trailing text
        # up to the next "$" / ";" / ". " ("$5 per extra vehicle"), so a neighbouring add-on
        # clause disqualifies only its own amount, not the site fee beside it.
        head = text[max(0, match.start() - 25) : match.start()]
        tail = text[match.end() :]
        stops = [pos for pos in (tail.find("$"), tail.find(";"), tail.find(". ")) if pos != -1]
        context = head + " " + tail[: min(min(stops, default=len(tail)), 30)]
        return not _NON_NIGHTLY_CONTEXT.search(context)

    amounts = sorted(
        value
        for match in _DOLLARS.finditer(text)
        if (value := float(match.group(1))) <= _MAX_PLAUSIBLE_NIGHTLY_USD and is_nightly(match)
    )
    if not amounts:
        return None, None
    return amounts[0], amounts[-1]


def _free_from_fee(fee: str | None) -> bool | None:
    """TRUE when the fee text explicitly says no charge, or every amount it names is $0;
    otherwise unknown (None) - never guessed as paid.

    A blob that names a real charge anywhere ("No Fee day use ... Camping : $8") is *not* free
    even though it also contains a free marker, so the parsed range gets the final say.
    """
    if not fee:
        return None
    low, high = _fee_range(fee)
    if high:  # names a positive amount somewhere -> a charge applies, marker notwithstanding
        return None
    if low == 0.0 and high == 0.0:
        return True
    return True if any(marker in fee.lower() for marker in _FREE_MARKERS) else None


def _query_centers(lat: float, lng: float, radius_km: float, query_radius_km: float) -> list[tuple[float, float]]:
    """Grid of query-circle centers covering the home disk.

    Circles of ``query_radius_km`` on a square grid of that same spacing fully tile the
    plane (worst-case gap is spacing·√2/2 < radius), so the whole disk is covered. Centers
    whose circle cannot reach the disk are dropped.
    """
    spacing_km = query_radius_km
    dlat_deg = spacing_km / KM_PER_DEG_LAT
    km_per_deg_lng = KM_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.01)
    dlng_deg = spacing_km / km_per_deg_lng
    steps = max(1, math.ceil(radius_km / spacing_km))

    centers: list[tuple[float, float]] = []
    for ilat in range(-steps, steps + 1):
        for ilng in range(-steps, steps + 1):
            center_lat = lat + ilat * dlat_deg
            center_lng = lng + ilng * dlng_deg
            # Keep the center only if its query circle can overlap the home disk.
            if haversine_km(lat, lng, center_lat, center_lng) <= radius_km + query_radius_km:
                centers.append((center_lat, center_lng))

    if len(centers) > _MAX_QUERY_CENTERS:
        centers.sort(key=lambda center: haversine_km(lat, lng, center[0], center[1]))
        logger.warning(
            "camps: radius %.0f km needs %d RIDB query circles, capping at %d closest to home",
            radius_km,
            len(centers),
            _MAX_QUERY_CENTERS,
        )
        centers = centers[:_MAX_QUERY_CENTERS]
    return centers


# USPS codes for the ``cfg.coverage`` region names (US states + DC). The RIDB ``state`` filter
# wants the two-letter code; coverage regions carry only the name.
_STATE_CODES: dict[str, str] = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
}


def _states_for_disk(coverage: Sequence[CoverageRegion], lat: float, lng: float, radius_km: float) -> list[str]:
    """USPS codes of the coverage regions whose bbox reaches within ``radius_km`` of home.

    The home disk's bounding box is tested for overlap against each region's
    ``(west, south, east, north)`` bbox - a cheap superset of "the disk touches the state",
    which is all we need to decide whether to list that state's facilities. Regions with no
    bbox or no known code are skipped; an empty result means "not in the US, use tiling".
    """
    dlat = radius_km / KM_PER_DEG_LAT
    dlng = radius_km / (KM_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.01))
    disk_w, disk_e = lng - dlng, lng + dlng
    disk_s, disk_n = lat - dlat, lat + dlat
    codes: list[str] = []
    for region in coverage:
        code = _STATE_CODES.get(region.name)
        if code is None or region.bbox is None:
            continue
        west, south, east, north = region.bbox
        if west <= disk_e and east >= disk_w and south <= disk_n and north >= disk_s:
            codes.append(code)
    return codes


def _coverage_state_codes(coverage: Sequence[CoverageRegion]) -> list[str]:
    """USPS codes for every ``cfg.coverage`` region that maps to a US state.

    The coverage-wide camp ingest (issue #306 workstream B) lists *all* of these, not just the
    ones whose bbox reaches the home disk (``_states_for_disk``) - a destination anywhere in
    coverage should have its campgrounds cached, not only ones near the configured home.
    """
    seen: set[str] = set()
    codes: list[str] = []
    for region in coverage:
        code = _STATE_CODES.get(region.name)
        if code is not None and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _parse_facility(record: dict[str, Any]) -> tuple[Any, ...] | None:
    """RIDB facility record -> a campsites row tuple, or None if it lacks usable coords."""
    facility_id = record.get("FacilityID")
    raw_lat = record.get("FacilityLatitude")
    raw_lng = record.get("FacilityLongitude")
    if not facility_id or raw_lat in (None, "") or raw_lng in (None, ""):
        return None
    lat, lng = float(raw_lat), float(raw_lng)
    if lat == 0.0 and lng == 0.0:  # RIDB uses 0,0 as "no coordinates"
        return None
    fee = _clean_text(record.get("FacilityUseFeeDescription"))
    fee_low, fee_high = _fee_range(fee)
    reservable = record.get("Reservable")  # bool with full=true, absent otherwise
    return (
        f"ridb:{facility_id}",
        record.get("FacilityName") or f"Facility {facility_id}",
        "campground",
        fee,
        _free_from_fee(fee),
        lat,
        lng,
        "ridb",
        f"https://www.recreation.gov/camping/campgrounds/{facility_id}",
        reservable if isinstance(reservable, bool) else None,
        fee_low,
        fee_high,
    )


def _get_page(
    client: httpx.Client,
    throttle: Throttle,
    params: dict[str, Any],
    headers: dict[str, str],
    *,
    attempts: int = 5,
    base_delay: float = 2.0,
) -> httpx.Response:
    """GET one page, pacing requests and backing off on a 429 (honoring Retry-After)."""
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    resp = None
    for attempt in range(1, attempts + 1):
        throttle.wait()
        resp = client.get(RIDB_FACILITIES, params=params, headers=headers)
        if resp.status_code != 429 or attempt == attempts:
            break
        time.sleep(retry_after_seconds(resp, attempt, base_delay=base_delay))
    assert resp is not None
    resp.raise_for_status()
    return resp


def _iter_facilities(
    client: httpx.Client,
    throttle: Throttle,
    api_key: str,
    query: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Yield every CAMPING facility RIDB returns for one query, paging by offset.

    ``query`` carries the scope filter - ``{"state": "OR"}`` for the per-state listing or
    ``{"latitude": .., "longitude": .., "radius": ..}`` for the fallback radius search.
    """
    offset = 0
    while True:
        resp = _get_page(
            client,
            throttle,
            params={**query, "activity": "CAMPING", "limit": _PAGE_SIZE, "offset": offset},
            headers={"apikey": api_key, "User-Agent": USER_AGENT},
        )
        payload = resp.json()
        records = payload.get("RECDATA", [])
        if not records:
            return
        yield from records
        offset += len(records)
        total = payload.get("METADATA", {}).get("RESULTS", {}).get("TOTAL_COUNT")
        if (total is not None and offset >= int(total)) or len(records) < _PAGE_SIZE:
            return


def _query_scopes(lat: float, lng: float, radius_km: float, states: Sequence[str]) -> list[tuple[str, dict[str, Any]]]:
    """(label, RIDB query params) pairs to page through - per-state when states resolved,
    otherwise the fallback radius tiling."""
    if states:
        # full=true so each record carries `Reservable` (the radius fallback stays lean - a
        # non-US home rarely hits it and doesn't get the attribute either way).
        return [(code, {"state": code, "full": "true"}) for code in states]
    query_radius_mi = _QUERY_RADIUS_MI
    return [
        (f"{clat:.3f},{clng:.3f}", {"latitude": clat, "longitude": clng, "radius": query_radius_mi})
        for clat, clng in _query_centers(lat, lng, radius_km, _QUERY_RADIUS_MI * _KM_PER_MILE)
    ]


def fetch_campsites(
    *,
    lat: float,
    lng: float,
    radius_km: float,
    api_key: str,
    states: Sequence[str] = (),
    clip: bool = True,
    client: httpx.Client | None = None,
    min_interval: float = _MIN_REQUEST_INTERVAL,
    progress_cb: Callable[[str, float], None] | None = None,
) -> list[tuple[Any, ...]]:
    """Fetch developed campgrounds, deduped by facility id.

    ``states`` is the USPS codes to list (``_states_for_disk``); empty falls back to the
    radius-tiling path for a non-US home. ``clip`` (default) drops any facility outside
    ``radius_km`` of (``lat``, ``lng``) - the coverage-wide ingest passes ``clip=False`` to
    keep every facility a listed state returns.
    """
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    throttle = Throttle(min_interval)
    by_id: dict[str, tuple[Any, ...]] = {}
    scopes = _query_scopes(lat, lng, radius_km, states)
    try:
        for index, (label, query) in enumerate(scopes):
            if progress_cb:
                progress_cb(
                    f"Fetching campgrounds ({index + 1}/{len(scopes)}: {label})…",
                    ((index + 1) / len(scopes)) * 100.0 if scopes else 100.0,
                )
            for record in _iter_facilities(client, throttle, api_key, query):
                row = _parse_facility(record)
                if row is None:
                    continue
                site_lat, site_lng = row[5], row[6]
                if not clip or haversine_km(lat, lng, site_lat, site_lng) <= radius_km:
                    by_id[row[0]] = row
    except SOURCE_ERRORS as error:
        # Match the land / trails / dispersed ingests: a RIDB outage (or a malformed page)
        # degrades to "no campgrounds this run" rather than aborting the whole refresh. Any
        # facilities already collected from earlier scopes are kept.
        logger.warning("camps: RIDB fetch failed (%s) - keeping %d sites gathered so far", error, len(by_id))
    finally:
        if owns:
            client.close()
    return list(by_id.values())


def ingest_campgrounds(
    cfg: Settings,
    con: psycopg.Connection | None = None,
    *,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Ingest developed campgrounds into the cache. Returns rows upserted (0 if no key)."""
    api_key = api_key or os.getenv("RIDB_API_KEY")
    if not api_key:
        logger.info("camps: RIDB_API_KEY unset - skipping campground ingest")
        return 0
    home = cfg.home
    states = _states_for_disk(cfg.coverage, home.lat, home.lng, home.radius_km)
    logger.info(
        "camps: %s",
        f"listing {len(states)} states ({', '.join(states)})" if states else "no US state resolved - radius tiling",
    )
    count = run_area_ingest(
        cfg,
        con,
        prefix="camps:ridb:",
        label="camps",
        noun="Campgrounds",
        fetch=lambda **kw: fetch_campsites(api_key=api_key, client=client, states=states, **kw),
        upsert=upsert_campsites,
        progress_cb=progress_cb,
    )
    # The home-radius prune would delete every row the coverage-wide ingest cached outside the
    # home disk, so it only owns pruning when no coverage is configured (the coverage path runs
    # ``prune_campsites_outside_bounds`` instead).
    if not cfg.coverage:
        with cache.connection(con) as db:
            pruned = cache.prune_campsites_outside_radius(db, "ridb", home.lat, home.lng, home.radius_km)
        if pruned:
            logger.info("camps: pruned %d ridb rows now outside the %.0f km home radius", pruned, home.radius_km)
    return count


# Bump when the coverage-wide RIDB query changes in a way that needs a re-pull; the marker
# ``camps:coverage:v{N}`` then stops matching and the next ``refresh --with camps --all`` cron
# re-lists every state on its own (issue #306 workstream B, same self-heal as trails).
_CAMPS_COVERAGE_VERSION = 1


def ingest_campgrounds_coverage(
    cfg: Settings,
    con: psycopg.Connection | None = None,
    *,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """List every developed campground across all of ``cfg.coverage``, per state.

    RIDB already fetches per-state, so full coverage is just listing every coverage state
    rather than only the ones near home (``_states_for_disk``). One-shot per query version:
    skips once ``camps:coverage:v{N}`` is in ``ingest_log``. Returns rows upserted (0 if no key).
    """
    api_key = api_key or os.getenv("RIDB_API_KEY")
    if not api_key:
        logger.info("camps: RIDB_API_KEY unset - skipping coverage-wide campground ingest")
        return 0
    states = _coverage_state_codes(cfg.coverage)
    if not states:
        logger.info("camps: no US state in coverage - nothing to list coverage-wide")
        return 0
    key = f"camps:coverage:v{_CAMPS_COVERAGE_VERSION}"
    with cache.connection(con) as db:
        if cache.is_ingested(db, key):
            logger.info("camps: coverage-wide campgrounds already ingested at v%d, skipping", _CAMPS_COVERAGE_VERSION)
            if progress_cb:
                progress_cb("Campgrounds already cached, skipping…", 100.0)
            return 0
        logger.info("camps: listing campgrounds across %d coverage states (%s)…", len(states), ", ".join(states))
        rows = fetch_campsites(
            lat=0.0,
            lng=0.0,
            radius_km=0.0,
            api_key=api_key,
            states=states,
            clip=False,
            client=client,
            progress_cb=progress_cb,
        )
        upsert_campsites(db, rows)
        cache.record_ingest(db, key, len(rows))
        pruned = 0
        # Only prune when every coverage region has a bbox: with a mixed config the envelope
        # omits the no-bbox regions, so pruning to it would delete campsites those regions
        # legitimately listed. Better to leave a few stale rows than drop valid ones.
        if cfg.coverage and all(region.bbox for region in cfg.coverage):
            west, south, east, north = coverage_envelope(cfg.coverage)
            pruned = cache.prune_campsites_outside_bounds(db, "ridb", west, south, east, north)
        # Drop markers from superseded query versions so ingest_log doesn't accrete a stale row.
        db.execute("DELETE FROM ingest_log WHERE key LIKE %s AND key <> %s", ["camps:coverage:v%", key])
    logger.info("camps: cached %d campgrounds coverage-wide (pruned %d outside the envelope)", len(rows), pruned)
    return len(rows)
