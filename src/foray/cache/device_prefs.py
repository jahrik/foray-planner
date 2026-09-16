"""Per-device (anonymous cookie) preferences: the "Set location" override and selected
genus filter (issue #79 Phase 2)."""

from __future__ import annotations

from typing import Any

import psycopg


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


def load_genera(con: psycopg.Connection, device_id: str) -> list[int]:
    """This device's selected genus taxon_ids. Empty means "everything nearby", not "none"."""
    rows = con.execute("SELECT taxon_id FROM app_genera WHERE device_id = %s", [device_id]).fetchall()
    return [row[0] for row in rows]


def list_selected_genera(con: psycopg.Connection, device_id: str) -> list[dict[str, Any]]:
    """This device's selected genera with their catalog names, for chip display."""
    rows = con.execute(
        """
        SELECT fungi_genera.taxon_id, fungi_genera.name, fungi_genera.common_name
        FROM app_genera
        JOIN fungi_genera ON fungi_genera.taxon_id = app_genera.taxon_id
        WHERE app_genera.device_id = %s
        ORDER BY fungi_genera.name
        """,
        [device_id],
    ).fetchall()
    return [{"taxon_id": taxon_id, "name": name, "common_name": common_name} for taxon_id, name, common_name in rows]


def add_genus(con: psycopg.Connection, device_id: str, taxon_id: int) -> None:
    con.execute(
        "INSERT INTO app_genera (device_id, taxon_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        [device_id, taxon_id],
    )


def remove_genus(con: psycopg.Connection, device_id: str, taxon_id: int) -> None:
    con.execute("DELETE FROM app_genera WHERE device_id = %s AND taxon_id = %s", [device_id, taxon_id])
