"""`foray` command-line entry point."""

from __future__ import annotations

import datetime as dt

import click

from foray import alerting, jobs
from foray.cache import connect, maybe_rebuild_phenology, observation_count, upsert_fungi_genera
from foray.config import Settings
from foray.logging_config import setup_logging
from foray.refresh import REFRESH_LAYERS, parse_month_list, run_home_refresh
from foray.scoring import build_phenology, plan_route
from foray.sources import fire, geocode, satellite
from foray.sources.camps import ingest_campgrounds, ingest_campgrounds_coverage
from foray.sources.dispersed import ingest_dispersed, ingest_dispersed_coverage
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
from foray.sources.trails import backfill_forage_obs, ingest_trails, ingest_trails_region


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Plan mushroom-hunting trips from iNaturalist phenology."""
    setup_logging()
    ctx.ensure_object(dict)
    cfg = Settings()
    ctx.obj["cfg"] = cfg
    alerting.init_sentry(cfg)


@cli.command("migrate")
def migrate_cmd() -> None:
    """Apply the schema + `_MIGRATIONS` chain and exit (issue #332). `connect()` already does
    this on every call, so this command exists to give CD a dedicated, one-shot migration step
    (run once, before any app/cron container using the new image starts) instead of every
    cron container and API instance racing `apply_schema` against each other and against a
    schema-breaking migration mid-rollout - see infra/ansible/tasks/deploy/migrate.yml."""
    con = connect()
    con.close()
    click.echo("Schema up to date.")


@cli.command("job", context_settings={"ignore_unknown_options": True})
@click.argument("name")
@click.option(
    "--writer",
    is_flag=True,
    help="Gate this run behind the writer-cap semaphore (issue #332 PR 2) - set for any job "
    "that writes to Postgres, so a pile-up of concurrent jobs can't overrun the writer cap "
    "(FORAY_OBSERVABILITY__WRITER_CAP).",
)
@click.argument("command_args", nargs=-1, type=click.UNPROCESSED)
def job_cmd(name: str, writer: bool, command_args: tuple[str, ...]) -> None:
    """Run a scheduled `foray` command through the job wrapper (issue #332): advisory-lock
    overlap guard, a `job_runs` row on completion, healthchecks.io pings, and a `foray alert`
    on failure. Usage: `foray job <name> -- <foray-subcommand> [args...]`, e.g.
    `foray job fire -- fire` or `foray job precip-backfill -- backfill-precip --no-rebuild`."""
    if not command_args:
        raise click.UsageError("job needs a command to run, e.g. `foray job fire -- fire`")
    exit_code = jobs.run(name, list(command_args), writer=writer)
    if exit_code:
        raise SystemExit(exit_code)


@cli.command("scheduler")
def scheduler_cmd() -> None:
    """Run the dev-loop scheduler (issue #332 PR 2): reads `jobs.yaml` (the same manifest the
    prod systemd timers are generated from) and runs each job through `foray job` on its own
    interval, forever. Replaces the old `scripts/scheduler.sh` - see `foray.schedule`."""
    from foray import schedule

    schedule.run_scheduler()


@cli.command("alert")
@click.argument("level")
@click.argument("message")
@click.pass_context
def alert_cmd(ctx: click.Context, level: str, message: str) -> None:
    """Deliver a domain alert (a REPLACE lane wiped, backlog growing, `www/maintenance.on`
    left set, cert renewal failed, ...) via the configured ntfy topic - always logged locally
    too. `level` is free text (e.g. `info`, `warning`, `error`); no-op delivery when
    `FORAY_OBSERVABILITY__NTFY_URL` is unset."""
    alerting.alert(ctx.obj["cfg"], level, message)


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
        total = 0
        if countries:
            for country in cfg.countries:
                click.echo(f"Ingesting {country.name} (place_id={country.place_id})…")
                counts = ingest_region(cfg, con, country)
                total += sum(counts.values())
                click.echo(f"  {sum(counts.values())} observations across {len(counts)} genera")
        elif all_regions:
            for region in cfg.coverage:
                click.echo(f"Ingesting {region.name} (place_id={region.place_id})…")
                counts = ingest_region(cfg, con, region)
                total += sum(counts.values())
                click.echo(f"  {sum(counts.values())} observations across {len(counts)} genera")
        elif region_name:
            click.echo(f"Ingesting {resolved_region.name} (place_id={resolved_region.place_id})…")
            counts = ingest_region(cfg, con, resolved_region)
            total += sum(counts.values())
            click.echo(f"  {sum(counts.values())} observations across {len(counts)} genera")
        else:
            click.echo(f"Ingesting Fungi observations within {cfg.home.radius_km} km of home…")
            counts = ingest(cfg, con)
            total += sum(counts.values())
            click.echo(f"  {sum(counts.values())} observations across {len(counts)} genera")

        if maybe_rebuild_phenology(con, cfg, total):
            click.echo("Rebuilding phenology…")
        click.echo(f"Total observations cached: {observation_count(con)}")
        jobs.emit_rows(total)
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
@click.option(
    "--all", "all_coverage", is_flag=True, help="Ingest reported dispersed camping across all coverage regions."
)
@click.pass_context
def dispersed_cmd(ctx: click.Context, all_coverage: bool) -> None:
    """Ingest OSM-reported dispersed camping (camp_site/camp_pitch/backcountry tags) near home,
    or --all (coverage-wide, issue #327/#332 - not wired into any prod schedule before this)."""
    cfg = ctx.obj["cfg"]
    con = connect()
    try:
        if all_coverage:
            count = ingest_dispersed_coverage(cfg, con)
            click.echo(f"Cached {count} reported dispersed-camping sites (coverage-wide).")
        else:
            count = ingest_dispersed(cfg, con)
            click.echo(f"Cached {count} reported dispersed-camping sites within {cfg.home.radius_km} km of home.")
    finally:
        con.close()


@cli.command("trails")
@click.option("--all", "all_coverage", is_flag=True, help="Ingest trails for every configured coverage region.")
@click.option(
    "--force",
    is_flag=True,
    help="Re-fetch coverage regions even if already ingested at the current query version "
    "(for OSM data drift or debugging - a query change re-pulls on its own via _TRAILS_QUERY_VERSION).",
)
@click.pass_context
def trails_cmd(ctx: click.Context, all_coverage: bool, force: bool) -> None:
    """Ingest OSM trails (paths, forest roads, hiking routes, trailheads) near home, or --all."""
    cfg = ctx.obj["cfg"]
    if all_coverage and not cfg.coverage:
        raise click.UsageError("No coverage regions configured (set FORAY_COVERAGE).")
    if force and not all_coverage:
        raise click.UsageError("--force only applies to --all (it re-pulls versioned coverage-region markers).")
    con = connect()
    try:
        if all_coverage:
            for region in cfg.coverage:
                click.echo(f"Ingesting trails for {region.name}…")
                count = ingest_trails_region(region, con, force=force)
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
            jobs.emit_rows(0)
            return
        total_checked = sum(genus_stats["checked"] for genus_stats in stats.values())
        total_purged = sum(genus_stats["purged"] for genus_stats in stats.values())
        total_reassigned = sum(genus_stats["reassigned"] for genus_stats in stats.values())
        click.echo(
            f"Revalidated {len(stats)} suspect genera: {total_checked} observations checked, "
            f"{total_purged} purged (no longer Fungi), {total_reassigned} reassigned."
        )
        jobs.emit_rows(total_checked)
        # Rebuild whenever any suspect genus actually had cached rows to check, not just when
        # something was purged - a row that stayed Fungi but got its lat/lng/observed_on
        # refreshed (cache.upsert_observations) can still shift which region/month bucket it
        # falls into, and a reassignment changes its taxon_id outright. Gating on purges alone
        # left phenology/regions stale after a refresh-only or reassign-only run.
        if total_checked and maybe_rebuild_phenology(con, cfg, total_checked):
            click.echo("Rebuilding phenology…")
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
        if updated and rebuild and maybe_rebuild_phenology(con, cfg, updated):
            click.echo("Rebuilding phenology…")
        jobs.emit_rows(updated)
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
        if updated and rebuild and maybe_rebuild_phenology(con, cfg, updated):
            click.echo("Rebuilding phenology…")
    finally:
        con.close()


@cli.command("backfill-forage")
@click.option("--limit", type=int, default=None, help="Cap trails re-counted this run (default: one batch).")
def backfill_forage_cmd(limit: int | None) -> None:
    """Refresh `trails.forage_obs` - the count of research-grade fungi observations hugging each
    trail line, a genus-agnostic "how productive is this ground" signal the map ramps and the
    Trails tab shows. One bounded pass per run (oldest counts first); a frequent cron cycles the
    whole table as observations drift. Purely set-based, no external calls."""
    con = connect()
    try:
        updated = backfill_forage_obs(con, max_trails=limit)
        click.echo(f"Recomputed foraging density for {updated} trails.")
    finally:
        con.close()


@cli.command("backfill-satellite")
@click.option("--limit", type=int, default=None, help="Cap regions fetched this run (default: all outstanding).")
@click.option(
    "--concurrency",
    type=int,
    default=4,
    help="Regions fetched in parallel. Each region also fans out to a handful of tile workers "
    "(see sources.satellite); Esri's tile CDN throttles a wide fan-out, so keep this modest.",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Clear region_satellite first and re-fetch every region. Use after a change to what a "
    "region's raster should contain (bbox, zoom, tile sources, compositing in sources.satellite) "
    "- a plain run only fills regions that are still missing.",
)
@click.pass_context
def backfill_satellite_cmd(ctx: click.Context, limit: int | None, concurrency: int, refresh: bool) -> None:
    """Fetch + cache the satellite fill (#293 follow-up) for every region that doesn't have it
    yet, so a destination's map selection never waits on a live tile fetch. Safe to re-run -
    a plain run only fetches regions still missing from `region_satellite`; `--refresh` clears
    the table first. Esri throttles a wide tile fan-out, so a full run is paced, not instant."""
    cfg = ctx.obj["cfg"]
    con = connect()
    try:
        updated, failed = satellite.backfill_region_satellite(
            con,
            cell_deg=cfg.cell_deg,
            max_regions=limit,
            concurrency=concurrency,
            refresh=refresh,
            progress_cb=lambda region_id, done, total: click.echo(f"[{done}/{total}] {region_id}"),
        )
        click.echo(f"Cached satellite imagery for {updated} regions ({failed} failed).")
        if failed and failed > updated:
            raise click.ClickException(f"{failed}/{updated + failed} regions failed - likely Esri throttling; re-run.")
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
            jobs.emit_rows(total_checked)
            ctx.exit(1)
        if total_checked == 0:
            click.echo("Nothing to resync.")
            jobs.emit_rows(0)
            return
        # `checked` is the batch size (`stale_observation_ids` always returns up to
        # `batch_size` rows regardless of whether any of them actually changed), so it's a poor
        # debounce signal for the hourly small-batch cron - it would cross the threshold on
        # nearly every tick. `purged`/`reassigned` are genuine taxon-bucket-changing writes.
        rebuilt = maybe_rebuild_phenology(con, cfg, total_purged + total_reassigned)
        click.echo(
            f"Done: {total_checked} observations checked, {total_purged} purged, "
            f"{total_reassigned} reassigned." + (" Rebuilt phenology." if rebuilt else "")
        )
        jobs.emit_rows(total_checked)
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
        jobs.emit_rows(len(rows))
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
        "Ingest region-scoped targets (mushrooms, camps, land, dispersed, trails) across all "
        "configured coverage/countries instead of just the home radius."
    ),
)
@click.pass_context
def refresh(ctx: click.Context, with_: str, all_coverage: bool) -> None:
    """Ingest observations + campgrounds + land + dispersed + trails, then (re)build phenology."""
    cfg = ctx.obj["cfg"]
    targets = _parse_targets(with_)
    if all_coverage:
        if "mushrooms" in targets and not cfg.countries:
            raise click.UsageError("No countries configured (set FORAY_COUNTRIES).")
        if any(t in targets for t in ("camps", "land", "dispersed", "trails")) and not cfg.coverage:
            raise click.UsageError("No coverage regions configured (set FORAY_COVERAGE).")
        # land / dispersed / trails query by bbox (envelope or per-region tiles); without one on
        # any region they'd raise a bare ValueError. camps only needs the region name -> state code.
        if any(t in targets for t in ("land", "dispersed", "trails")) and not any(r.bbox for r in cfg.coverage):
            raise click.UsageError("No coverage region has a bbox (needed for land/dispersed/trails --all).")
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
            if "camps" in targets:
                click.echo("Ingesting campgrounds across coverage…")
                ingest_campgrounds_coverage(cfg, con)
            if "land" in targets:
                ingest_public_land_coverage(cfg, con)
            if "dispersed" in targets:
                click.echo("Ingesting dispersed camping across coverage…")
                ingest_dispersed_coverage(cfg, con)
            if "trails" in targets:
                # Skip observation-only regions (a `countries`-style entry with no bbox); the
                # per-region Overpass ingest needs one and would otherwise raise.
                for region in cfg.coverage:
                    if region.bbox is None:
                        continue
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
            ttl_seconds=cfg.observability.ranking_cache_ttl_seconds,
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
