#!/usr/bin/env python3
"""Scratch script: stream-filter the full iNat DwC-A dump down to every genus in our catalog
(US only).

Not part of the foray package - for exploring the bulk-download path before deciding how (or
whether) to wire it into the real ingest pipeline. Reads/writes the gitignored `data/` dir;
this script itself lives under version control in `scripts/`.

`observations.csv` inside the zip is ~208M rows / 106.85GB uncompressed - this never extracts
the zip or that CSV to disk. zipfile.open() streams+decompresses the entry on the fly, and
csv.reader consumes it lazily, so memory stays flat regardless of file size. Only matching rows
(known genus, countryCode=="US", has coordinates) get written out.

The dump itself (see eml.xml's alternateIdentifier) is already iNaturalist's
"quality_grade=research" export - every row here is research-grade at the source, so the output
rows all carry quality_grade="research" (issue #108's scoring filter needs that to count them).

This will take a while (fully scanning ~208M rows) - a tqdm progress bar tracks bytes read off
the zip entry's decompressed stream against its exact uncompressed size (from the zip's own
central directory, via ZipInfo.file_size), so the percentage/ETA are accurate without guessing
a row count.

Usage: make bulk-download bulk-filter (or `uv run python scripts/inat_dwca_filter.py` directly)
Output: data/inat_us_observations.jsonl (one JSON object per matching record)
"""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import BinaryIO, cast

from tqdm import tqdm
from tqdm.utils import CallbackIOWrapper

from foray.cache import connect, genus_taxon_ids

DATA_DIR = Path(__file__).parent.parent / "data"
ZIP_PATH = DATA_DIR / "gbif-observations-dwca.zip"
OUTPUT_PATH = DATA_DIR / "inat_us_observations.jsonl"
ENTRY_NAME = "observations.csv"

# Column indices from meta.xml's field order for the Occurrence core (0-indexed, matches
# observations.csv's header exactly - verified by reading both directly out of the zip).
COL_ID = 0
COL_EVENT_DATE = 16
COL_LAT = 20
COL_LNG = 21
COL_COORD_UNCERTAINTY = 22
COL_COUNTRY_CODE = 24
COL_TAXON_ID = 29
COL_GENUS = 37


def _load_genus_taxon_ids() -> dict[str, int]:
    """Full genus name -> taxon_id map from the `fungi_genera` catalog (populate it first with
    `foray genera-refresh`)."""
    con = connect()
    try:
        genera = genus_taxon_ids(con)
    finally:
        con.close()
    if not genera:
        sys.exit("fungi_genera catalog is empty - run `foray genera-refresh` first.")
    return genera


def main() -> None:
    if not ZIP_PATH.exists():
        sys.exit(f"Missing {ZIP_PATH} - run `make bulk-download` first.")

    taxon_id_by_genus = _load_genus_taxon_ids()
    print(f"Loaded {len(taxon_id_by_genus):,} genera from the catalog.", flush=True)

    kept = 0
    scanned = 0

    with zipfile.ZipFile(ZIP_PATH) as zf, zf.open(ENTRY_NAME) as raw:
        entry_size = zf.getinfo(ENTRY_NAME).file_size  # exact uncompressed size, from the zip itself

        with tqdm(total=entry_size, unit="B", unit_scale=True, unit_divisor=1024, desc=ENTRY_NAME) as bar:
            # CallbackIOWrapper proxies every attribute to `raw` (including close/readable/etc via
            # ObjectWrapper.__getattr__), so it satisfies TextIOWrapper's buffer protocol at
            # runtime even though the type checker can't see that through the dynamic proxy.
            wrapped = cast(BinaryIO, CallbackIOWrapper(bar.update, raw, "read"))
            # errors="replace": a single bad byte shouldn't abort a multi-hour scan of an
            # external archive we don't control.
            text = io.TextIOWrapper(wrapped, encoding="utf-8", newline="", errors="replace")
            reader = csv.reader(text)
            header = next(reader)
            expected_len = len(header)

            with OUTPUT_PATH.open("w") as out:
                for row in reader:
                    scanned += 1

                    if len(row) != expected_len:
                        # Malformed/truncated row - skip rather than crash a multi-hour run over it.
                        continue

                    genus = row[COL_GENUS]
                    taxon_id = taxon_id_by_genus.get(genus)
                    if taxon_id is None:
                        continue
                    if row[COL_COUNTRY_CODE] != "US":
                        continue
                    lat, lng = row[COL_LAT], row[COL_LNG]
                    if not lat or not lng:
                        continue

                    out.write(
                        json.dumps(
                            {
                                "inat_id": int(row[COL_ID]),
                                "genus": genus,
                                "taxon_id": taxon_id,
                                "inat_taxon_id": row[COL_TAXON_ID] or None,
                                "lat": float(lat),
                                "lng": float(lng),
                                "coordinate_uncertainty_m": row[COL_COORD_UNCERTAINTY] or None,
                                "event_date": row[COL_EVENT_DATE] or None,
                            }
                        )
                        + "\n"
                    )
                    kept += 1
                    bar.set_postfix(kept=kept, refresh=False)

    print(f"Done. scanned={scanned:,} kept={kept:,}")
    print(f"Output: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
