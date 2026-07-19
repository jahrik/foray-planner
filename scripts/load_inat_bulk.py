#!/usr/bin/env python3
"""Scratch script: bulk-load the filtered iNat DwC-A dump into Postgres.

Reads data/inat_us_observations.jsonl (produced by inat_dwca_filter.py) and:
  1. Upserts every row into `observations` via cache.upsert_observations() - idempotent
     (ON CONFLICT DO UPDATE on id; existing lat/lng/date/quality fields from a live ingest
     are never clobbered, only place_guess/uri/obscured get backfilled when missing).
     quality_grade is always written as "research": the GBIF DwC-A dump itself is already
     iNaturalist's quality_grade=research export (see the zip's eml.xml), so every row here
     is research-grade at the source - needed for issue #108's scoring filter to count them.
  2. Writes one ingest_log row per genus in the same "obs:{taxon_id}:place:{place_id}:
     {start}:{end}" shape ingest_region() itself produces, so the nightly cron (now capped
     to region_sync_days - see ingest.py) treats these genera as already covered and only
     syncs forward from here instead of re-attempting a full backfill.

Connects to Postgres via foray.cache.connect(), i.e. the standard PG* env vars/libpq
conninfo - point it at prod by exporting PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE
(e.g. `set -a; source foray.env; set +a`) before running, or leave unset for local dev.

Not part of the foray package - reads/writes the gitignored `data/` dir; this script itself
lives under version control in `scripts/`.

Usage: make bulk-load (or `uv run python scripts/load_inat_bulk.py` directly)
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from tqdm import tqdm

from foray.cache import connect, record_ingest, upsert_observations

INPUT_PATH = Path(__file__).parent.parent / "data" / "inat_us_observations.jsonl"
PLACE_ID_US = 1  # matches defaults.COUNTRIES's "United States" entry
SINCE_YEAR_FLOOR = "2000-01-01"  # older than iNat itself - marks these genera as fully backfilled
CHUNK_SIZE = 5000


def _parse_date(event_date: str | None) -> dt.date | None:
    if not event_date:
        return None
    try:
        return dt.date.fromisoformat(event_date[:10])
    except ValueError:
        return None


def main() -> None:
    if not INPUT_PATH.exists():
        raise SystemExit(f"Missing {INPUT_PATH} - run `make bulk-filter` first.")

    con = connect()

    chunk: list[tuple[Any, ...]] = []
    per_taxon_count: dict[int, int] = defaultdict(int)
    per_taxon_max_date: dict[int, dt.date] = {}
    total = 0
    skipped_no_date = 0

    with INPUT_PATH.open() as input_file, tqdm(input_file, unit=" obs", unit_scale=True) as bar:
        for line in bar:
            rec = json.loads(line)
            day = _parse_date(rec.get("event_date"))
            if day is None:
                skipped_no_date += 1
                continue

            taxon_id = rec["taxon_id"]
            uncertainty = rec.get("coordinate_uncertainty_m")
            row = (
                rec["inat_id"],
                taxon_id,
                rec["lat"],
                rec["lng"],
                day,
                day.month,
                day.year,
                "research",  # dump is already quality_grade=research at the source
                int(float(uncertainty)) if uncertainty else None,
                None,  # place_guess
                f"https://www.inaturalist.org/observations/{rec['inat_id']}",
                None,  # obscured
            )
            chunk.append(row)
            per_taxon_count[taxon_id] += 1
            if taxon_id not in per_taxon_max_date or day > per_taxon_max_date[taxon_id]:
                per_taxon_max_date[taxon_id] = day

            if len(chunk) >= CHUNK_SIZE:
                upsert_observations(con, chunk)
                total += len(chunk)
                chunk = []
                bar.set_postfix(upserted=total)

        if chunk:
            upsert_observations(con, chunk)
            total += len(chunk)
            bar.set_postfix(upserted=total)

    print(
        f"\nUpserted {total:,} observations across {len(per_taxon_count)} genera ({skipped_no_date} skipped, no date)."
    )

    for taxon_id, count in sorted(per_taxon_count.items()):
        end_date = per_taxon_max_date[taxon_id].isoformat()
        key = f"obs:{taxon_id}:place:{PLACE_ID_US}:{SINCE_YEAR_FLOOR}:{end_date}"
        record_ingest(con, key, count)
        print(f"  taxon {taxon_id}: {count:,} rows, ingest_log marked through {end_date}")


if __name__ == "__main__":
    main()
