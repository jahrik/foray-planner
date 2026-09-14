"""Bulk-snapshot iNat loader (issue #334 PR 2) - the "biggest win" from the S1 survey.

Streams iNaturalist's own complete GBIF Darwin Core Archive export
(``https://static.inaturalist.org/observations/gbif-observations-dwca.zip``, ~29 GB,
refreshed at least daily) straight off HTTP via ``foray.sources.http.HttpRangeReader``, never
downloading the whole archive - only ``observations.csv`` (the DwC Occurrence core) is read,
filtered down to Fungi/US rows, and staged as a small Parquet file (issue #359 PR 1 - the
standard bulk-snapshot format, via ``foray.spaces.write_snapshot_parquet``/
``read_snapshot_parquet``). This is the same DwC-A export ``scripts/inat_dwca_filter.py``/
``load_inat_bulk.py`` used manually (see git history) - this module replaces both with a
repeatable, Space-backed pipeline any droplet (or CI runner) can run via
``foray stage-snapshot inat`` / ``foray ingest-bulk inat``, instead of a human running
``just bulk-download``/``bulk-filter``/``bulk-load`` by hand.

**Why not the AWS Open Data dump** (``inaturalist-open-data``, the other bulk source TODO.md
flagged for "confirm"): checked live 2026-09-12 - it's TSV, not Parquet, and critically its
``observations.csv.gz`` keys rows by ``observation_uuid`` only, never the numeric iNat ``id``
this project's ``observations.id BIGINT PRIMARY KEY`` (and every downstream join - resync,
revalidate, elevation/precip backfill) assumes. The GBIF DwC-A export's Occurrence core carries
the numeric id directly (its ``id`` field, DwC ``occurrenceID`` - column 0, verified against a
live streamed read), so it's the only bulk source with a schema this table can load without a
much larger re-keying migration.

**Why kingdom, not the ``fungi_genera`` catalog:** the old filter script matched each row's
``genus`` name against a preloaded catalog (needing a live DB connection at filter time,
before any row was even known to be Fungi). The DwC-A dump carries ``kingdom`` directly (field
32, confirmed against the archive's own ``meta.xml``), so the *stager* (which runs with no DB -
GitHub Actions, not the droplet) filters on ``kingdom == "Fungi"`` instead - broader than a
genus whitelist (catches genera not yet in the catalog too) and self-contained. Resolving each
surviving row's ``genus`` name to *our* genus-level ``taxon_id`` (what ``observations.taxon_id``
actually stores - see ``foray.sources.ingest``'s module docstring) still needs
``fungi_genera``, so that lookup moves to the *loader* (``load_inat``), which does have a
connection; a row whose genus isn't cataloged yet is skipped, same as the old script.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import zipfile
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import psycopg
import pyarrow as pa

from foray import spaces
from foray.cache import genus_taxon_ids, insert_observations_if_missing, maybe_rebuild_phenology, record_ingest
from foray.config import Settings
from foray.sources.http import USER_AGENT, HttpRangeReader, Throttle
from foray.sources.inat import OBSCURED_ACCURACY_HIGH, OBSCURED_ACCURACY_LOW

logger = logging.getLogger(__name__)

DWCA_URL = "https://static.inaturalist.org/observations/gbif-observations-dwca.zip"
DWCA_ENTRY = "observations.csv"

# Column indices into observations.csv's header, from the archive's own meta.xml (0-indexed) -
# same source scripts/inat_dwca_filter.py's COL_* constants used, plus COL_KINGDOM (32), which
# the old script never needed since it filtered by genus name instead.
_COL_ID = 0
_COL_EVENT_DATE = 16
_COL_LAT = 20
_COL_LNG = 21
_COL_COORD_UNCERTAINTY = 22
_COL_COUNTRY_CODE = 24
_COL_KINGDOM = 32
_COL_GENUS = 37

# matches scripts/load_inat_bulk.py's marker place/floor, so /api/coverage keeps reading the
# same key shape regardless of which loader populated it.
_PLACE_ID_US = 1
_SINCE_YEAR_FLOOR = "2000-01-01"

# 64 MiB, not http.py's usual 8 MiB (see camps.py's RIDB reader) - the ~29 GB DwC-A scan makes
# thousands of range GETs at 8 MiB each, which is what got the client 403'd by
# static.inaturalist.org partway through a run (seen in production, see HttpRangeReader's
# docstring); an 8x larger buffer cuts the request count (and _RANGE_MIN_INTERVAL's added wall
# time) by the same factor for the same bytes read.
_BUFFER_SIZE = 64 * 1024 * 1024
_CHUNK_SIZE = 5000

_SNAPSHOT_FILENAME = "fungi_us.parquet"
# event_date/coordinate_uncertainty_m stay as the raw CSV strings (matching what this module has
# always staged) rather than parsing them here - `load_inat` already does that parsing
# (`_parse_date`, the `int(float(...))` accuracy conversion) and a malformed value should fail
# there, at load time with the genus/DB context available, not silently drop the row at stage
# time before that context exists.
_SNAPSHOT_SCHEMA = pa.schema(
    [
        ("id", pa.int64()),
        ("genus", pa.string()),
        ("lat", pa.float64()),
        ("lng", pa.float64()),
        ("event_date", pa.string()),
        ("coordinate_uncertainty_m", pa.string()),
    ]
)

# Paces successive range GETs against static.inaturalist.org - see HttpRangeReader's docstring.
# 0.25s is a guess at "comfortably under whatever burst threshold triggered the 403", not a
# documented limit (iNat doesn't publish one for this static export); revisit if staging still
# gets blocked, or relax it if a run comfortably completes with room to spare.
_RANGE_MIN_INTERVAL = 0.25


def _open_observations_csv(client: httpx.Client) -> tuple[zipfile.ZipFile, io.TextIOWrapper]:
    reader = HttpRangeReader(client, DWCA_URL, throttle=Throttle(_RANGE_MIN_INTERVAL))
    buffered = io.BufferedReader(reader, buffer_size=_BUFFER_SIZE)
    zf = zipfile.ZipFile(buffered)
    raw = zf.open(DWCA_ENTRY)
    # errors="replace": a single bad byte in a ~29 GB archive we don't control shouldn't abort
    # a run that's otherwise streamed cleanly (same guard scripts/inat_dwca_filter.py used).
    return zf, io.TextIOWrapper(raw, encoding="utf-8", newline="", errors="replace")


def iter_fungi_us_rows(client: httpx.Client) -> Iterator[dict[str, Any]]:
    """Stream ``observations.csv`` out of the live DwC-A archive, yielding one dict per
    Fungi-kingdom, US, coordinate-bearing row: ``{id, genus, lat, lng, event_date,
    coordinate_uncertainty_m}``. Never materializes the archive or the full CSV on disk."""
    zf, text = _open_observations_csv(client)
    with zf, text:
        reader = csv.reader(text)
        header = next(reader)
        expected_len = len(header)
        scanned = kept = 0
        for row in reader:
            scanned += 1
            if len(row) != expected_len:
                continue  # malformed/truncated row - skip rather than abort a multi-hour scan
            if row[_COL_KINGDOM] != "Fungi" or row[_COL_COUNTRY_CODE] != "US":
                continue
            lat, lng = row[_COL_LAT], row[_COL_LNG]
            if not lat or not lng:
                continue
            kept += 1
            yield {
                "id": int(row[_COL_ID]),
                "genus": row[_COL_GENUS],
                "lat": float(lat),
                "lng": float(lng),
                "event_date": row[_COL_EVENT_DATE] or None,
                "coordinate_uncertainty_m": row[_COL_COORD_UNCERTAINTY] or None,
            }
            if kept % 100_000 == 0:
                logger.info("inat_bulk: scanned %d rows, kept %d Fungi/US so far", scanned, kept)
        logger.info("inat_bulk: scan done - %d rows scanned, %d Fungi/US kept", scanned, kept)


def stage_inat(cfg: Settings, snapshot_date: date, run_id: str) -> None:
    """Stager: stream-filter the live DwC-A dump to Fungi/US rows and upload as a Parquet file
    under this run's Space prefix. No DB connection - see this module's docstring for why
    genus->taxon_id resolution happens in ``load_inat`` instead. Runs in GitHub Actions."""
    with httpx.Client(timeout=120.0, headers={"User-Agent": USER_AGENT}) as client:
        kept = spaces.write_snapshot_parquet(
            cfg.spaces,
            "inat",
            snapshot_date,
            run_id,
            _SNAPSHOT_FILENAME,
            iter_fungi_us_rows(client),
            _SNAPSHOT_SCHEMA,
        )
    logger.info("inat_bulk: staged %d Fungi/US observations from the DwC-A export", kept)


def _parse_date(event_date: str | None) -> dt.date | None:
    if not event_date:
        return None
    try:
        return dt.date.fromisoformat(event_date[:10])
    except ValueError:
        return None


def load_inat(con: psycopg.Connection, cfg: Settings, snapshot_date: date, run_id: str) -> None:
    """Loader: resolve each staged row's genus to our catalog's genus-level taxon_id and
    insert it into ``observations`` if not already cached (``cache.insert_observations_if_missing``
    - never overwrites an existing row, see that function's docstring for why this must be
    insert-only rather than an upsert), in the shape ``foray.sources.ingest`` writes
    (``quality_grade="research"`` - the DwC-A dump is already iNat's research-grade export;
    ``obscured`` set via the coordinate-uncertainty fingerprint heuristic, since the dump - like
    the old script's - carries no real obscured flag). Records one whole-kingdom ingest_log
    marker so the nightly incremental crawl (``ingest``/``ingest_region``) treats US coverage
    as already backfilled through the newest date loaded, and only syncs forward from there;
    also triggers the debounced phenology rebuild (``cache.maybe_rebuild_phenology``) since
    nothing else does after a bulk load this size.
    """
    genera = genus_taxon_ids(con)
    if not genera:
        raise RuntimeError("fungi_genera catalog is empty - run `foray genera-refresh` first")
    total = 0
    skipped_unknown_genus = 0
    skipped_no_date = 0
    max_date: dt.date | None = None
    for batch in spaces.read_snapshot_parquet(
        cfg.spaces, "inat", snapshot_date, run_id, _SNAPSHOT_FILENAME, batch_size=_CHUNK_SIZE
    ):
        chunk: list[tuple[Any, ...]] = []
        for rec in batch:
            taxon_id = genera.get(rec["genus"])
            if taxon_id is None:
                skipped_unknown_genus += 1
                continue
            day = _parse_date(rec["event_date"])
            if day is None:
                skipped_no_date += 1
                continue
            uncertainty = rec.get("coordinate_uncertainty_m")
            accuracy = int(float(uncertainty)) if uncertainty else None
            obscured = True if accuracy and OBSCURED_ACCURACY_LOW <= accuracy <= OBSCURED_ACCURACY_HIGH else None
            chunk.append(
                (
                    rec["id"],
                    taxon_id,
                    rec["lat"],
                    rec["lng"],
                    day,
                    day.month,
                    "research",
                    accuracy,
                    None,  # place_guess
                    f"https://www.inaturalist.org/observations/{rec['id']}",
                    obscured,
                )
            )
            if max_date is None or day > max_date:
                max_date = day
        if chunk:
            insert_observations_if_missing(con, chunk)
            total += len(chunk)
    logger.info(
        "inat_bulk: loaded %d observations (%d unknown genus, %d no date)",
        total,
        skipped_unknown_genus,
        skipped_no_date,
    )
    if max_date is not None:
        ingest_key = f"obs:fungi:place:{_PLACE_ID_US}:{_SINCE_YEAR_FLOOR}:{max_date.isoformat()}"
        record_ingest(con, ingest_key, total)
        logger.info("inat_bulk: marked place %d covered through %s", _PLACE_ID_US, max_date.isoformat())
    # A bulk load can seed hundreds of thousands of rows in one pass - unlike the live
    # ingest/ingest_region path (which each call maybe_rebuild_phenology themselves after every
    # run), nothing else rebuilds regions/phenology from this loader, so rankings would keep
    # reading pre-bulk tables until an unrelated ingest happened to cross the threshold on its
    # own. Feed the same debounced rebuild path directly.
    if total and maybe_rebuild_phenology(con, cfg, total):
        logger.info("inat_bulk: phenology rebuild triggered by this load")
