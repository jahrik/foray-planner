"""Per-device (anonymous cookie) preferences: the "Set location" override and selected
target taxa (issues #79, #464)."""

from __future__ import annotations

from typing import Any

import psycopg

from foray.cache.taxa import taxon_labels


def load_location(con: psycopg.Connection, device_id: str) -> dict[str, Any] | None:
    """This device's "Set location" override, if one has been saved. `None` = use the default."""
    row = con.execute("SELECT name, lat, lng, radius_km FROM app_location WHERE device_id = %s", [device_id]).fetchone()
    if row is None:
        return None
    name, lat, lng, radius_km = row
    return {"name": name, "lat": lat, "lng": lng, "radius_km": radius_km}


def save_location(
    con: psycopg.Connection, *, device_id: str, name: str, lat: float, lng: float, radius_km: float
) -> None:
    """Save (insert or replace) a device's home name, coordinates and search radius."""
    con.execute(
        """
        INSERT INTO app_location (device_id, name, lat, lng, radius_km)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (device_id) DO UPDATE SET
            name = EXCLUDED.name,
            lat = EXCLUDED.lat,
            lng = EXCLUDED.lng,
            radius_km = EXCLUDED.radius_km
        """,
        [device_id, name, lat, lng, radius_km],
    )


def delete_location(con: psycopg.Connection, device_id: str) -> None:
    """Issue #81: let a visitor delete their saved "Set location" override outright."""
    con.execute("DELETE FROM app_location WHERE device_id = %s", [device_id])


def load_targets(con: psycopg.Connection, device_id: str) -> list[int]:
    """This device's picked target taxon ids (any rank). Empty means "everything nearby", not "none"."""
    rows = con.execute("SELECT taxon_id FROM app_targets WHERE device_id = %s", [device_id]).fetchall()
    return [row[0] for row in rows]


def list_selected_targets(con: psycopg.Connection, device_id: str) -> list[dict[str, Any]]:
    """This device's picked targets with their catalog names, for chip display."""
    ids = load_targets(con, device_id)
    labels = taxon_labels(con, ids)
    return sorted(
        ({"taxon_id": taxon_id, **labels[taxon_id]} for taxon_id in ids if taxon_id in labels),
        key=lambda target: target["name"],
    )


def add_target(con: psycopg.Connection, device_id: str, taxon_id: int) -> None:
    """Add a taxon (any rank) to a device's target list; adding one already there is a no-op."""
    con.execute(
        "INSERT INTO app_targets (device_id, taxon_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        [device_id, taxon_id],
    )


def remove_target(con: psycopg.Connection, device_id: str, taxon_id: int) -> None:
    """Remove a taxon from a device's target list; removing one not there is a no-op."""
    con.execute("DELETE FROM app_targets WHERE device_id = %s AND taxon_id = %s", [device_id, taxon_id])
