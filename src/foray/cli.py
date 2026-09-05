"""`foray` command-line entry point."""

from __future__ import annotations

import datetime as dt

import click

from foray.cache import connect, observation_count, upsert_fungi_genera
from foray.config import Settings
from foray.logging_config import setup_logging
from foray.refresh import REFRESH_LAYERS, parse_month_list, run_home_refresh
from foray.scoring import build_phenology, plan_route
from foray.sources import fire, geocode, satellite
from foray.sources.camps import ingest_campgrounds
from foray.sources.dispersed import ingest_dispersed
from foray.sources.inat import InatQuotaExceeded, iter_fungi_genera
from foray.sources.ingest import (
    backfill_elevations,
    backfill_precip,
    ingest,
    ingest_region,
    refresh_precipitation,
    resync,
    revalidate,
)
from foray.sources.land import ingest_public_land, ingest_public_land_coverage
from foray.sources.trails import ingest_trails, ingest_trails_region


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Plan mushroom-hunting trips from iNaturalist phenology."""
    setup_logging()
    ctx.ensure_object(dict)
    ctx.obj["cfg"] = Settings()


@cli.command("ingest")
@click.option(
    "--region",
    "region_name",
    default=None,
    help="Named coverage region to ingest (from FORAY_COVERAGE).",
)
@click.option("--all-regions", "all_regions", is_flag=True, help="Ingest all configured coverage regions.")
@click.option("--countries", "countries", is_flag=True, help="Ingest all configured countries (one query per country).")
@click.pass_context
def ingest_cmd(ctx: click.Context, region_name: str | None, all_regions: bool, countries: bool) -> None:
    """Pull observations into the cache (home radius, --region/--all-regions, or --countries)."""
    cfg = ctx.obj["cfg"]
    if sum([bool(region_name), all_regions, countries]) > 1:
        raise click.UsageError("Use only one of --region, --all-regions, --countries.")
    if all_regions and not cfg.coverage:
        raise click.UsageError("No coverage regions configured (set FORAY_COVERAGE).")
    if countries and not cfg.countries:
        raise click.UsageError("No countries configured (set FORAY_COUNTRIES).")
    if region_name:
        resolved_region = next((r for r in cfg.coverage if r.name.lower() == region_name.lower()), None)
        if resolved_region is None:
            available = ", ".join(r.name for r in cfg.coverage) or "(none configured)"
            raise click.UsageError(f"Unknown region {region_name!r}. Available: {available}")

    con = connect()
    try:
        if countries:
            for country in cfg.countries:
                click.echo(f"Ingesting {country.name} (place_id={country.place_id})…")
                counts = ingest_region(cfg, con, country)
                click.echo(f"  {sum(counts.values())} observations across {len(counts)} genera")
        elif all_regions:
            for region in cfg.coverage:
                click.echo(f"Ingesting {region.name} (place_id={region.place_id})…")
                counts = ingest_region(cfg, con, region)
                click.echo(f"  {sum(counts.values())} observations across {len(counts)} genera")
        elif region_name:
            click.echo(f"Ingesting {resolved_region.name} (place_id={resolved_region.place_id})…")
            counts = ingest_region(cfg, con, resolved_region)
            click.echo(f"  {sum(counts.values())} observations across {len(counts)} genera")
        else:
            click.echo(f"Ingesting Fungi observations within {cfg.home.radius_km} km of home…")
            counts = ingest(cfg, con)
            click.echo(f"  {sum(counts.values())} observations across {len(counts)} genera")

        click.echo("Rebuilding phenology…")
        build_phenology(con, cfg.cell_deg)
        click.echo(f"Total observations cached: {observation_count(con)}")
    finally:
        con.close()


@cli.command("camps")
@click.pass_context
def camps_cmd(ctx: click.Context) -> None:
    """Ingest developed campgrounds (Recreation.gov RIDB) within the home radius."""
    cfg = ctx.obj["cfg"]
    con = connect()
    try:
        count = ingest_campgrounds(cfg, con)
        if count:
            click.echo(f"Cached {count} campgrounds within {cfg.home.radius_km} km of home.")
        else:
            click.echo("No campgrounds ingested - set RIDB_API_KEY to enable camp data.")
    finally:
        con.close()


@cli.command("land")
@click.option(
    "--all", "all_coverage", is_flag=True, help="Ingest BLM/USFS ownership across all coverage regions in one query."
)
@click.pass_context
def land_cmd(ctx: click.Context, all_coverage: bool) -> None:
    """Ingest public-land ownership (BLM + USFS) polygons within the home radius, or --all."""
    cfg = ctx.obj["cfg"]
    con = connect()
    try:
        if all_coverage:
            count = ingest_public_land_coverage(cfg, con)
            click.echo(f"Cached {count} public-land units (coverage-wide).")
        else:
            count = ingest_public_land(cfg, con)
            click.echo(f"Cached {count} public-land units within {cfg.home.radius_km} km of home.")
    finally:
        con.close()


@cli.command("dispersed")
@click.pass_context
def dispersed_cmd(ctx: click.Context) -> None:
    """Ingest OSM-reported dispersed camping (camp_site/camp_pitch/backcountry tags) near home."""
    cfg = ctx.obj["cfg"]
    con = connect()
    try:
        count = ingest_dispersed(cfg, con)
        click.echo(f"Cached {count} reported dispersed-camping sites within {cfg.home.radius_km} km of home.")
    finally:
        con.close()


@cli.command("trails")
@click.option("--all", "all_coverage", is_flag=True, help="Ingest trails for every configured coverage region.")
@click.pass_context
def trails_cmd(ctx: click.Context, all_coverage: bool) -> None:
    """Ingest OSM trails (paths, hiking routes, trailheads) near home, or --all."""
    cfg = ctx.obj["cfg"]
    if all_coverage and not cfg.coverage:
        raise click.UsageError("No coverage regions configured (set FORAY_COVERAGE).")
    con = connect()
    try:
        if all_coverage:
            for region in cfg.coverage:
                click.echo(f"Ingesting trails for {region.name}…")
                count = ingest_trails_region(region, con)
                click.echo(f"  cached {count} trails")
        else:
            count = ingest_trails(cfg, con)
            click.echo(f"Cached {count} trails within {cfg.home.radius_km} km of home.")
    finally:
        con.close()


@cli.command("revalidate")
@click.pass_context
def revalidate_cmd(ctx: click.Context) -> None:
    """Re-check cached observations under genera whose cache count has drifted from iNat's
    live count - purges/reassigns rows misidentified into a homonymous non-fungal genus (e.g.
    fungal Olla vs. the ladybug genus Olla) that iNat corrected but this cache never saw.
    Meant to run on a schedule (see scripts/scheduler.sh), not just once."""
    cfg = ctx.obj["cfg"]
    con = connect()
    try:
        try:
            stats = revalidate(cfg, con)
        except InatQuotaExceeded as exc:
            click.echo(str(exc), err=True)
            ctx.exit(1)
        if not stats:
            click.echo("No suspect genera found - nothing to revalidate.")
            return
        total_checked = sum(genus_stats["checked"] for genus_stats in stats.values())
        total_purged = sum(genus_stats["purged"] for genus_stats in stats.values())
        total_reassigned = sum(genus_stats["reassigned"] for genus_stats in stats.values())
        click.echo(
            f"Revalidated {len(stats)} suspect genera: {total_checked} observations checked, "
            f"{total_purged} purged (no longer Fungi), {total_reassigned} reassigned."
        )
        # Rebuild whenever any suspect genus actually had cached rows to check, not just when
        # something was purged - a row that stayed Fungi but got its lat/lng/observed_on
        # refreshed (cache.upsert_observations) can still shift which region/month bucket it
        # falls into, and a reassignment changes its taxon_id outright. Gating on purges alone
        # left phenology/regions stale after a refresh-only or reassign-only run.
        if total_checked:
            click.echo("Rebuilding phenology…")
            build_phenology(con, cfg.cell_deg)
    finally:
        con.close()


@cli.command("backfill-elevation")
@click.option("--limit", type=int, default=None, help="Cap observations enriched this run (default: all outstanding).")
@click.option(
    "--rebuild/--no-rebuild",
    default=True,
    help="Rebuild phenology afterward so cards pick up the new region means (default). "
    "The hourly prod cron passes --no-rebuild - the daily ingest rebuild covers it.",
)
@click.pass_context
def backfill_elevation_cmd(ctx: click.Context, limit: int | None, rebuild: bool) -> None:
    """Fill in `elevation_m` for cached observations that don't have it yet (issue #36), from
    Open-Meteo's DEM. Ingest does this automatically for new rows; run this to drain the
    backlog. Drains as fast as the free tier allows, then stops - re-run (or let the cron)
    pick up the rest."""
    cfg = ctx.obj["cfg"]
    con = connect()
    try:
        updated = backfill_elevations(con, max_points=limit)
        click.echo(f"Enriched {updated} observations with elevation.")
        if updated and rebuild:
            click.echo("Rebuilding phenology…")
            build_phenology(con, cfg.cell_deg)
    finally:
        con.close()


@cli.command("backfill-precip")
@click.option("--limit", type=int, default=None, help="Cap grid cells enriched this run (default: all pending).")
@click.option(
    "--rebuild/--no-rebuild",
    default=True,
    help="Rebuild phenology afterward so cards pick up the new region rain means (default).",
)
@click.pass_context
def backfill_precip_cmd(ctx: click.Context, limit: int | None, rebuild: bool) -> None:
    """Fill in `precip_7d_mm` / `precip_30d_mm` for cached observations that don't have them yet
    (issue #226), from Open-Meteo's ERA5 archive. Ingest does this automatically for new rows;
    run this to drain the backlog. A window still inside ERA5's ~5-7 day lag stays NULL and is
    retried next run."""
    cfg = ctx.obj["cfg"]
    con = connect()
    try:
        updated = backfill_precip(con, cell_deg=cfg.cell_deg, max_cells=limit)
        click.echo(f"Enriched {updated} observations with antecedent rainfall.")
        if updated and rebuild:
            click.echo("Rebuilding phenology…")
            build_phenology(con, cfg.cell_deg)
    finally:
        con.close()


@cli.command("backfill-satellite")
@click.option("--limit", type=int, default=None, help="Cap regions fetched this run (default: all outstanding).")
@click.option(
    "--concurrency", type=int, default=8, help="Parallel Esri exports in flight (each takes 25-45s server-side)."
)
@click.pass_context
def backfill_satellite_cmd(ctx: click.Context, limit: int | None, concurrency: int) -> None:
    """Fetch + cache the satellite fill (#293 follow-up) for every region that doesn't have it
    yet, so a destination's map selection never pays Esri's ~30-45s live render time. Safe to
    re-run - only fetches regions still missing from `region_satellite`. This is a genuinely
    slow, one-time-per-region operation (thousands of regions x tens of seconds each even at
    default concurrency); run it in the background and let it drain over hours, not inline."""
    cfg = ctx.obj["cfg"]
    con = connect()
    try:
        updated = satellite.backfill_region_satellite(
            con,
            cell_deg=cfg.cell_deg,
            max_regions=limit,
            concurrency=concurrency,
            progress_cb=lambda region_id, done, total: click.echo(f"[{done}/{total}] {region_id}"),
        )
        click.echo(f"Cached satellite imagery for {updated} regions.")
    finally:
        con.close()


@cli.command("refresh-precip")
@click.pass_context
def refresh_precip_cmd(ctx: click.Context) -> None:
    """Refresh the recent-rainfall-per-destination layer (issue #226 Part 2): pull the trailing
    ~30 days from Open-Meteo's forecast API for every active region cell. Runs on its own
    scheduler cadence (FORAY_PRECIP_INTERVAL_HOURS)."""
    cfg = ctx.obj["cfg"]
    con = connect()
    try:
        written = refresh_precipitation(con, cfg)
        click.echo(f"Refreshed recent rainfall for {written} regions.")
    finally:
        con.close()


@cli.command("fire")
@click.pass_context
def fire_cmd(ctx: click.Context) -> None:
    """Refresh wildfire perimeters + recent burn scars (issue #227) from the NIFC / MTBS ArcGIS
    services: active fires (replace semantics), the last 3+current fire years of history, and
    MTBS burn severity. Runs on its own scheduler cadence (FORAY_FIRE_INTERVAL_HOURS)."""
    cfg = ctx.obj["cfg"]
    con = connect()
    try:
        counts = fire.refresh_fire(con, cfg)
        click.echo(
            f"Fire: {counts['active']} active + {counts['points']} points, "
            f"{counts['history']} historical, MTBS severity on {counts['severity']}."
        )
    finally:
        con.close()


@cli.command("resync")
@click.option(
    "--batch-size",
    default=2000,
    show_default=True,
    help="How many of the oldest/never-checked cached observations to re-fetch per batch.",
)
@click.option(
    "--until-done",
    is_flag=True,
    default=False,
    help=(
        "Keep resyncing batch after batch until every cached row has been live-checked at "
        "least once, instead of stopping after one batch. Meant for a deliberate catch-up run "
        "(e.g. right after finding a data-accuracy bug), not the normal recurring schedule - "
        "that stays on scripts/scheduler.sh's small-batch/hourly pace so it doesn't compete "
        "with other scheduled jobs for iNat's rate limit."
    ),
)
@click.pass_context
def resync_cmd(ctx: click.Context, batch_size: int, until_done: bool) -> None:
    """Re-check the observations cache against iNat, oldest/never-checked first - the only path
    that eventually trues up every column (including `obscured`, never set by the bulk
    historical import) and catches a misidentification too rare within its genus for
    `revalidate`'s ratio check to flag. Meant to run frequently in small batches on a schedule
    (see scripts/scheduler.sh), grinding through the whole cache over time - or pass
    --until-done for a one-off run that doesn't stop until the whole cache is caught up."""
    cfg = ctx.obj["cfg"]
    con = connect()
    try:
        # stale_observation_ids always returns up to `batch_size` rows (oldest-checked/never-
        # checked first, NULLS FIRST) with no "actually stale" filter - that's correct for the
        # recurring small-batch cron job, which is meant to grind forever, but it means
        # `checked` never drops below `batch_size` once every row has been checked at least
        # once. Stopping on `checked < batch_size` alone would never trigger then, looping
        # --until-done forever. Cap on a full lap instead: NULLS FIRST guarantees never-yet-
        # checked-this-run rows are always exhausted before any repeat appears, so once
        # cumulative `checked` reaches the row count observed at the start, every row that
        # existed then has been re-verified at least once (a handful of ids in the final batch
        # may be benign repeats, not missed rows).
        target = observation_count(con)
        total_checked = total_purged = total_reassigned = 0
        try:
            while True:
                result = resync(cfg, con, batch_size=batch_size)
                total_checked += result["checked"]
                total_purged += result["purged"]
                total_reassigned += result["reassigned"]
                if result["checked"]:
                    click.echo(
                        f"Resynced {result['checked']} observations: {result['purged']} purged "
                        f"(no longer Fungi/geolocatable), {result['reassigned']} reassigned. "
                        f"(running total: {total_checked} checked)"
                    )
                if not until_done or result["checked"] < batch_size or total_checked >= target:
                    break
        except InatQuotaExceeded as exc:
            click.echo(
                f"Stopped after {total_checked} checked ({total_purged} purged, {total_reassigned} reassigned) - {exc}",
                err=True,
            )
            ctx.exit(1)
        if total_checked == 0:
            click.echo("Nothing to resync.")
            return
        click.echo(
            f"Done: {total_checked} observations checked, {total_purged} purged, "
            f"{total_reassigned} reassigned. Rebuilding phenology…"
        )
        build_phenology(con, cfg.cell_deg)
    finally:
        con.close()


@cli.command("genera-refresh")
def genera_refresh_cmd() -> None:
    """Refresh the full Fungi genus catalog from iNat (issue #79's search/selection catalog)."""
    con = connect()
    try:
        rows = list(iter_fungi_genera())
        upsert_fungi_genera(
            con,
            [
                {
                    "taxon_id": row["id"],
                    "name": row["name"],
                    "common_name": row.get("preferred_common_name"),
                    "observations_count": row.get("observations_count"),
                }
                for row in rows
            ],
        )
        click.echo(f"Cached {len(rows)} Fungi genera.")
    finally:
        con.close()


def _parse_targets(with_: str) -> tuple[str, ...]:
    """Parse a comma-separated `--with` list, raising on unknown targets."""
    if not with_.strip():
        return REFRESH_LAYERS
    values = tuple(token.strip() for token in with_.split(",") if token.strip())
    unknown = [target for target in values if target not in REFRESH_LAYERS]
    if unknown:
        raise click.BadParameter(f"unknown target(s) {unknown} - choose from {', '.join(REFRESH_LAYERS)}")
    return values


@cli.command()
@click.option(
    "--with",
    "with_",
    default="",
    help=(
        "Comma-separated subset to warm: mushrooms,camps,land,dispersed,trails "
        "(default: all). e.g. --with camps,trails to prefetch offline layers only."
    ),
)
@click.option(
    "--all",
    "all_coverage",
    is_flag=True,
    help=(
        "Ingest region-scoped targets (mushrooms, land, trails) across all configured "
        "coverage/countries instead of just the home radius. Not supported for camps/dispersed "
        "(on-demand, home-radius only)."
    ),
)
@click.pass_context
def refresh(ctx: click.Context, with_: str, all_coverage: bool) -> None:
    """Ingest observations + campgrounds + land + dispersed + trails, then (re)build phenology."""
    cfg = ctx.obj["cfg"]
    targets = _parse_targets(with_)
    if all_coverage:
        unsupported = [t for t in targets if t in ("camps", "dispersed")]
        if unsupported:
            raise click.UsageError(f"--all doesn't apply to {', '.join(unsupported)} (home-radius only, on-demand).")
        if "mushrooms" in targets and not cfg.countries:
            raise click.UsageError("No countries configured (set FORAY_COUNTRIES).")
        if "land" in targets and not cfg.coverage:
            raise click.UsageError("No coverage regions configured (set FORAY_COVERAGE).")
        if "trails" in targets and not cfg.coverage:
            raise click.UsageError("No coverage regions configured (set FORAY_COVERAGE).")
    con = connect()
    try:
        # No more global location override to load here - home/radius overrides are now per-device
        # (anonymous cookie, see api.py), which this CLI path has no way to resolve. Cron-driven
        # refresh uses `cfg.home` (the env-configured default) unchanged; a redesign to close this
        # gap for background layer refresh is tracked separately.
        if all_coverage:
            if "mushrooms" in targets:
                for country in cfg.countries:
                    click.echo(f"Ingesting {country.name}…")
                    ingest_region(cfg, con, country)
            if "land" in targets:
                ingest_public_land_coverage(cfg, con)
            if "trails" in targets:
                for region in cfg.coverage:
                    click.echo(f"Ingesting trails for {region.name}…")
                    ingest_trails_region(region, con)
            # The home-radius path rebuilds phenology inside run_home_refresh; the
            # coverage-wide path doesn't use it, so rebuild here instead.
            if "mushrooms" in targets:
                build_phenology(con, cfg.cell_deg)
        else:
            run_home_refresh(cfg, con, targets)
        if "mushrooms" in targets:
            region_count = (con.execute("SELECT count(*) FROM regions").fetchone() or (0,))[0]
            click.echo(f"Phenology rebuilt across {region_count} regions ({observation_count(con)} observations).")
        else:
            click.echo(f"Warmed: {', '.join(targets)}.")
    finally:
        con.close()


def _parse_months(months: str) -> list[int]:
    """Parse a comma-separated month list, raising a Click error on junk or out-of-range values."""
    try:
        return parse_month_list(months)
    except ValueError as error:
        raise click.BadParameter(str(error)) from error


@cli.command("plan")
@click.option("--months", default="", help="Comma-separated months (1-12); default = current month.")
@click.option(
    "--destination",
    default=None,
    help="Trip destination: a place name or 'lat,lng'. Omit to auto-pick the best reachable region.",
)
@click.option(
    "--corridor-km",
    default=60.0,
    type=click.FloatRange(min=0, min_open=True),
    help="How far off the straight line home->destination a stop may be.",
)
@click.option("--max-stops", default=5, type=click.IntRange(min=1), help="Maximum stays in the itinerary.")
@click.option(
    "--max-drive-km",
    default=400.0,
    type=click.FloatRange(min=0, min_open=True),
    help="Max great-circle km per leg.",
)
@click.option("--any-camp", is_flag=True, help="Allow stops whose nearest camp isn't free-tagged.")
@click.pass_context
def plan_cmd(
    ctx: click.Context,
    months: str,
    destination: str | None,
    corridor_km: float,
    max_stops: int,
    max_drive_km: float,
    any_camp: bool,
) -> None:
    """Plan a trip from home to a destination (auto-picked if omitted), stopping along the way."""
    cfg = ctx.obj["cfg"]
    selected = _parse_months(months) or [dt.date.today().month]
    if destination is not None:
        try:
            dest_location = geocode.resolve(destination)
        except (LookupError, ValueError) as error:
            raise click.BadParameter(str(error), param_hint="destination") from error
        dest_lat, dest_lng = dest_location.lat, dest_location.lng
    else:
        dest_lat, dest_lng = None, None
    con = connect()
    try:
        trip = plan_route(
            con,
            months=selected,
            taxon_ids=[],  # no fixed target list (issue #79) - every genus in the catalog is in play
            cell_deg=cfg.cell_deg,
            start_lat=cfg.home.lat,
            start_lng=cfg.home.lng,
            destination_lat=dest_lat,
            destination_lng=dest_lng,
            corridor_km=corridor_km,
            auto_pick_radius_km=cfg.home.radius_km,
            recent_weeks=cfg.recent_weeks,
            max_stops=max_stops,
            max_drive_km=max_drive_km,
            require_free_camp=not any_camp,
        )
    finally:
        con.close()
    if not trip.stops:
        click.echo("No viable stops found - try a wider radius, more months, or --any-camp.")
        return
    dest_label = (
        f"region {trip.destination_name}"
        if trip.auto_destination
        else f"{trip.destination_lat:.4f}, {trip.destination_lng:.4f}"
    )
    click.echo(
        f"Trip from {cfg.home.name} to {dest_label} - months {selected}, {trip.n_stops} stops, "
        f"{trip.total_drive_km:.0f} km total drive:"
    )
    for stop in trip.stops:
        top = ", ".join(hit.common_name or hit.name for hit in stop.species[:3]) or "-"
        camp = (
            "no camp"
            if stop.camp is None
            else (f"{'FREE ' if stop.camp_is_free else ''}{stop.camp.name} ({stop.camp.distance_km:.0f} km)")
        )
        trail = "no trail" if stop.trail is None else f"{stop.trail.name} ({stop.trail.distance_km:.0f} km)"
        click.echo(
            f"  {stop.order}. {stop.region_id}  +{stop.drive_km_from_prev:.0f} km  "
            f"score {stop.score_norm:.2f}  {stop.n_species} spp [{top}]  camp: {camp}  trail: {trail}"
        )
    if trip.skipped_unreachable:
        click.echo(f"  ({trip.skipped_unreachable} viable stop(s) skipped - beyond max drive.)")


@cli.command()
@click.pass_context
def openapi(ctx: click.Context) -> None:
    """Print the FastAPI OpenAPI schema as JSON (feeds the frontend type generator)."""
    import json

    from foray.api import create_app

    click.echo(json.dumps(create_app(ctx.obj["cfg"]).openapi()))


@cli.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8000, type=int)
@click.pass_context
def serve(ctx: click.Context, host: str, port: int) -> None:
    """Run the web app."""
    import uvicorn

    from foray.api import create_app

    uvicorn.run(create_app(ctx.obj["cfg"]), host=host, port=port)


if __name__ == "__main__":
    cli()
