"""The trip planner: ``start`` -> ``destination`` with the best fruiting stops along the way."""

from __future__ import annotations

import psycopg

from foray.geo import haversine_km, project_to_plane, segment_progress_and_offset
from foray.scoring.models import CampSite, RegionScore, Stop, Trail, TripPlan
from foray.scoring.queries import camps_near, trails_near
from foray.scoring.ranking import rank_destinations, rank_destinations_corridor


def _region_center(region_id: str, cell_deg: float) -> tuple[float, float]:
    """Approximate (lat, lng) center of a grid region from its ``"ilat_ilng"`` id - the cell
    midpoint. Close enough for routing geometry (the real per-region center is the observation
    centroid, carried on the ``RegionScore`` once the region is ranked)."""
    ilat, ilng = region_id.split("_")
    return (int(ilat) + 0.5) * cell_deg, (int(ilng) + 0.5) * cell_deg


def plan_route(
    con: psycopg.Connection,
    *,
    months: list[int],
    taxon_ids: list[int],
    cell_deg: float,
    start_lat: float,
    start_lng: float,
    destination_lat: float | None = None,
    destination_lng: float | None = None,
    corridor_km: float = 60.0,
    auto_pick_radius_km: float = 300.0,
    auto_pick_min_km: float = 50.0,
    recent_weeks: int = 4,
    max_stops: int = 5,
    max_drive_km: float = 400.0,
    camp_radius_km: float = 40.0,
    require_free_camp: bool = False,
    min_score_norm: float = 0.0,
    waypoints: list[str] | None = None,
) -> TripPlan:
    """Plan a trip from ``start`` to ``destination`` (auto-picked if not given), stopping at the
    best fruiting spots - each with a nearby camp and trail - along the way.

    Two phases:

    1. **Pick a destination**, if the caller didn't supply one: run ``rank_destinations``
       radially from ``start`` within ``auto_pick_radius_km``, drop anything closer than
       ``auto_pick_min_km`` (don't auto-pick something next door - this is meant to be a
       trip), and take the best-scoring survivor. Nothing clearing that bar yields an empty
       plan rather than silently falling back to the old radial-only behaviour.
    2. **Select + order stops along the corridor**: ``rank_destinations_corridor`` (already
       score-desc, with ``distance_km`` repurposed as progress-along-line) - annotate each
       with its nearest campsite (``camps_near``, free-first) and nearest trail
       (``trails_near``), drop regions below ``min_score_norm`` or - when
       ``require_free_camp`` - without a free camp inside ``camp_radius_km``, keep the top
       ``max_stops`` by score, then sort by progress along the line ("along the way" order,
       not nearest-neighbour). Walking that order, any stop whose leg from the current position
       exceeds ``max_drive_km`` is skipped individually (counted in ``skipped_unreachable``)
       rather than assumed to make every later stop unreachable too - legs aren't guaranteed
       monotonic under progress ordering (see below). Great-circle distance stands in for real
       drive time until road routing lands (a documented follow-up).

    Straight-line v1: the "corridor" is a buffer around the great-circle chord, not a real
    road route, so a wide corridor with off-axis stops can occasionally zigzag rather than
    monotonically recede from the current position - this is exactly why unreachable stops are
    skipped individually rather than truncating the rest of the itinerary.

    ``waypoints`` is an ordered list of region ids the caller has hand-picked (the redesign's
    "+ Plan" shortlist): they are threaded in as **required** stops - never dropped for score,
    a missing free camp, or an over-long leg - and the remaining ``max_stops`` slots are
    auto-filled from the best other corridor regions. When no ``destination`` is given, the
    trip runs to the waypoint farthest from ``start`` (so the corridor spans the picks), and
    the corridor is widened as needed so every waypoint falls inside it.

    Missing tables (nothing ingested yet) surface as ``rank_destinations``/
    ``rank_destinations_corridor`` raising, mirroring the other modes; an empty candidate set
    yields an empty plan.
    """
    forced_ids = list(dict.fromkeys(waypoints or []))  # dedup, preserve caller order
    forced_set = set(forced_ids)
    auto = destination_lat is None or destination_lng is None
    destination_name: str | None = None
    if auto and forced_ids:
        # Span the picks: destination = the waypoint farthest from start.
        farthest = max(
            forced_ids,
            key=lambda region_id: haversine_km(start_lat, start_lng, *_region_center(region_id, cell_deg)),
        )
        destination_lat, destination_lng = _region_center(farthest, cell_deg)
        destination_name = farthest
    elif auto:
        picks = rank_destinations(
            con,
            months=months,
            taxon_ids=taxon_ids,
            home_lat=start_lat,
            home_lng=start_lng,
            radius_km=auto_pick_radius_km,
            cell_deg=cell_deg,
            recent_weeks=recent_weeks,
        )
        picks = [
            pick
            for pick in picks
            if haversine_km(start_lat, start_lng, pick.center_lat, pick.center_lng) >= auto_pick_min_km
        ]
        if not picks:
            return TripPlan(
                start_lat=start_lat,
                start_lng=start_lng,
                destination_lat=start_lat,
                destination_lng=start_lng,
                destination_name=None,
                auto_destination=True,
                corridor_km=corridor_km,
                months=months,
                n_stops=0,
                total_drive_km=0.0,
                stops=[],
                skipped_unreachable=0,
            )
        destination_lat, destination_lng = picks[0].center_lat, picks[0].center_lng
        destination_name = picks[0].region_id

    # Every path above either resolved a destination or returned early.
    assert destination_lat is not None and destination_lng is not None

    # Widen the corridor so every hand-picked waypoint falls inside the ranked candidate set.
    if forced_ids:
        dx, dy = project_to_plane(start_lat, start_lng, destination_lat, destination_lng)
        for region_id in forced_ids:
            way_lat, way_lng = _region_center(region_id, cell_deg)
            plane_x, plane_y = project_to_plane(start_lat, start_lng, way_lat, way_lng)
            _, offset_km = segment_progress_and_offset(plane_x, plane_y, dx, dy)
            corridor_km = max(corridor_km, offset_km + cell_deg * 111.0)

    ranked = rank_destinations_corridor(
        con,
        months=months,
        taxon_ids=taxon_ids,
        start_lat=start_lat,
        start_lng=start_lng,
        dest_lat=destination_lat,
        dest_lng=destination_lng,
        corridor_km=corridor_km,
        cell_deg=cell_deg,
        recent_weeks=recent_weeks,
    )

    # Select - annotate + filter, preserving the score-desc order rank_destinations_corridor
    # returns. Hand-picked waypoints (forced_set) are kept unconditionally; the rest fill the
    # remaining slots. Stop scanning once we have every waypoint and enough fill candidates.
    forced: list[tuple[RegionScore, CampSite | None, bool, Trail | None]] = []
    optional: list[tuple[RegionScore, CampSite | None, bool, Trail | None]] = []
    seen_forced: set[str] = set()
    for region in ranked:
        is_forced = region.region_id in forced_set
        if not is_forced and region.score_norm < min_score_norm:
            continue
        # camps_near ranks free-first, so its nearest result is the nearest *free* camp when one
        # is in range, else the nearest of any kind - one query answers both cases.
        nearby_camps = camps_near(con, lat=region.center_lat, lng=region.center_lng, radius_km=camp_radius_km)
        camp = nearby_camps[0] if nearby_camps else None
        camp_is_free = camp is not None and camp.free is True
        if require_free_camp and not camp_is_free and not is_forced:
            continue
        # Only the single nearest trail's geometry is needed here, and the plan does its own
        # camps_near - so skip trails_near's per-trail nearest-camp LATERAL (seconds per call
        # in trail-dense terrain) and let the query fetch just the one closest trail.
        nearby_trails = trails_near(
            con,
            lat=region.center_lat,
            lng=region.center_lng,
            radius_km=camp_radius_km,
            limit=1,
            with_camp_distance=False,
        )
        trail = nearby_trails[0] if nearby_trails else None
        if is_forced:
            forced.append((region, camp, camp_is_free, trail))
            seen_forced.add(region.region_id)
        else:
            optional.append((region, camp, camp_is_free, trail))
        if seen_forced >= forced_set and len(optional) >= max_stops:
            break

    # Keep every waypoint, then fill up to max_stops with the best remaining regions.
    fill = max(max_stops - len(forced), 0)
    candidates = forced + optional[:fill]

    # Order - by progress along the start->destination line ("along the way"), not nearest-neighbour.
    candidates.sort(key=lambda item: item[0].distance_km)

    cur_lat, cur_lng = start_lat, start_lng
    stops: list[Stop] = []
    cumulative = 0.0
    skipped = 0
    for region, camp, camp_is_free, trail in candidates:
        leg = haversine_km(cur_lat, cur_lng, region.center_lat, region.center_lng)
        if leg > max_drive_km and region.region_id not in forced_set:
            # Progress-ordered, not nearest-neighbour, so legs aren't guaranteed monotonic (a wide
            # corridor can zigzag off-axis) - skip just this stop rather than assuming everything
            # still to come is unreachable too. Hand-picked waypoints are kept regardless.
            skipped += 1
            continue
        cumulative += leg
        stops.append(
            Stop(
                order=len(stops) + 1,
                region_id=region.region_id,
                center_lat=region.center_lat,
                center_lng=region.center_lng,
                score_norm=region.score_norm,
                n_species=region.n_species,
                recent_count=region.recent_count,
                species=region.species,
                progress_km=region.distance_km,
                drive_km_from_prev=round(leg, 1),
                cumulative_drive_km=round(cumulative, 1),
                camp=camp,
                camp_is_free=camp_is_free,
                trail=trail,
                trail_distance_km=trail.distance_km if trail else None,
                # Active-fire warnings only - a burn scar near a stop isn't a hazard (issue #227).
                fire_nearby=[fire for fire in region.fire_nearby if fire.status == "active"],
            )
        )
        cur_lat, cur_lng = region.center_lat, region.center_lng

    return TripPlan(
        start_lat=start_lat,
        start_lng=start_lng,
        destination_lat=destination_lat,
        destination_lng=destination_lng,
        destination_name=destination_name,
        auto_destination=auto,
        corridor_km=corridor_km,
        months=months,
        n_stops=len(stops),
        total_drive_km=round(cumulative, 1),
        stops=stops,
        skipped_unreachable=skipped,
    )
