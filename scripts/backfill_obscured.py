#!/usr/bin/env python3
"""One-time script: heuristically flag ``obscured`` for cached rows that never got the real flag.

The ~1.9M-row bulk historical import never set `observations.obscured` (NULL), and
`scoring.py` treats NULL as "not obscured" (`obscured or False`) - so a geoprivacy-obscured
observation's cached point (iNat's *randomized decoy* coordinate, not the real find location)
gets served to the UI as if it were precise. Confirmed as a real user-facing bug, 2026-07-21: a
reported observation's cached location didn't match anywhere iNat shows that species actually
observed, because the point was the decoy.

`ingest.resync` (see foray.ingest) is the permanent, recurring fix - it re-fetches every cached
row from iNat over time and writes the real flag. But at its deliberately-small batch size that
takes weeks to reach every row, which is too slow to fix an already-reported bug promptly. This
script is a one-time stopgap: iNat's obscuration snaps a coordinate to a fixed-size grid cell,
producing a distinctive `positional_accuracy` value - empirically a 26,000-31,000m band in this
cache, measured 98.3% precise against the ~8k rows here whose real flag is already known from a
live fetch (4,441 true / 75 false in that exact band). Only ever sets TRUE, never FALSE, so it
can't wrongly un-obscure a row it misses; `resync` overwrites the ~1.7% false positives with the
real flag as it reaches them, same as any other row.

Cleanup note: this script's WHERE clause only touches `obscured IS NULL` rows, so it becomes a
natural no-op forever once `resync` has swept every row at least once (check: `make psql
SQL="SELECT count(*) FROM observations WHERE revalidated_at IS NULL"` == 0). At that point this
script - and its Makefile target - are dead and can be deleted. Not part of the `foray` package;
connects via foray.cache.connect() like scripts/load_inat_bulk.py.

Usage: make backfill-obscured (or `uv run python scripts/backfill_obscured.py` directly)
"""

from __future__ import annotations

from foray.cache import connect
from foray.inat import OBSCURED_ACCURACY_HIGH, OBSCURED_ACCURACY_LOW


def main() -> None:
    con = connect()
    with con.cursor() as cur:
        cur.execute(
            "UPDATE observations SET obscured = TRUE WHERE obscured IS NULL AND positional_accuracy BETWEEN %s AND %s",
            [OBSCURED_ACCURACY_LOW, OBSCURED_ACCURACY_HIGH],
        )
        updated = cur.rowcount
    print(f"Backfilled obscured=true for {updated:,} likely-obscured cached observations.")


if __name__ == "__main__":
    main()
