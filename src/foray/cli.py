"""`foray` command-line entry point."""

from __future__ import annotations

import datetime as dt
import logging

import click

from foray.cache import connect, load_location, observation_count
from foray.camps import ingest_campgrounds
from foray.config import Home, Settings
from foray.dispersed import ingest_dispersed
from foray.ingest import ingest, ingest_region
from foray.land import ingest_public_land
from foray.scoring import build_phenology, plan_route
from foray.trails import ingest_trails


def _setup_logging() -> None:
    """Send `foray.*` progress logs to stderr at INFO. Idempotent (safe to call twice)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Plan mushroom-hunting trips from iNaturalist phenology."""
    _setup_logging()
    ctx.ensure_object(dict)
    ctx.obj["cfg"] = Settings()


@cli.command("ingest")
@click.option(
    "--region",
    "region_name",
    default=None,
    help="Named coverage region to ingest (from FORAY_COVERAGE).",
)
@click.option(
    "--all-regions", "all_regions", is_flag=True, help="Ingest all configured coverage regions."
)
@click.pass_context
def ingest_cmd(ctx: click.Context, region_name: str | None, all_regions: bool) -> None:
    """Pull observations into the cache (home radius, or --region/--all-regions for place_id)."""
    cfg = ctx.obj["cfg"]
    if region_name and all_regions:
        raise click.UsageError("Use --region or --all-regions, not both.")
    if all_regions and not cfg.coverage:
        raise click.UsageError("No coverage regions configured (set FORAY_COVERAGE).")
    if region_name:
        resolved_region = next(
            (r for r in cfg.coverage if r.name.lower() == region_name.lower()), None
        )
        if resolved_region is None:
            available = ", ".join(r.name for r in cfg.coverage) or "(none configured)"
            raise click.UsageError(f"Unknown region {region_name!r}. Available: {available}")

    con = connect()
    try:
        if all_regions:
            for region in cfg.coverage:
                click.echo(f"Ingesting {region.name} (place_id={region.place_id})…")
                counts = ingest_region(cfg, con, region)
                for species in cfg.species:
                    click.echo(f"  {species.common_name:28s} {counts.get(species.taxon_id, 0):>6d}")
        elif region_name:
            click.echo(f"Ingesting {resolved_region.name} (place_id={resolved_region.place_id})…")
            counts = ingest_region(cfg, con, resolved_region)
            for species in cfg.species:
                click.echo(f"  {species.common_name:28s} {counts.get(species.taxon_id, 0):>6d}")
        else:
            click.echo(
                f"Ingesting {len(cfg.species)} species within {cfg.home.radius_km} km of home…"
            )
            counts = ingest(cfg, con)
            for species in cfg.species:
                click.echo(f"  {species.common_name:28s} {counts.get(species.taxon_id, 0):>6d}")

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
    count = ingest_campgrounds(cfg, con)
    if count:
        click.echo(f"Cached {count} campgrounds within {cfg.home.radius_km} km of home.")
    else:
        click.echo("No campgrounds ingested - set RIDB_API_KEY to enable camp data.")
    con.close()


@cli.command("land")
@click.pass_context
def land_cmd(ctx: click.Context) -> None:
    """Ingest public-land ownership (BLM + USFS) polygons within the home radius."""
    cfg = ctx.obj["cfg"]
    con = connect()
    count = ingest_public_land(cfg, con)
    click.echo(f"Cached {count} public-land units within {cfg.home.radius_km} km of home.")
    con.close()


@cli.command("dispersed")
@click.pass_context
def dispersed_cmd(ctx: click.Context) -> None:
    """Ingest OSM dispersed camping (reported sites + road∩public-land proxy) near home."""
    cfg = ctx.obj["cfg"]
    con = connect()
    count = ingest_dispersed(cfg, con)
    click.echo(f"Cached {count} dispersed/reported sites within {cfg.home.radius_km} km of home.")
    con.close()


@cli.command("trails")
@click.pass_context
def trails_cmd(ctx: click.Context) -> None:
    """Ingest OSM trails (paths, hiking routes, trailheads) near home."""
    cfg = ctx.obj["cfg"]
    con = connect()
    count = ingest_trails(cfg, con)
    click.echo(f"Cached {count} trails within {cfg.home.radius_km} km of home.")
    con.close()


_REFRESH_TARGETS = ("mushrooms", "camps", "land", "dispersed", "trails")


def _parse_targets(with_: str) -> tuple[str, ...]:
    """Parse a comma-separated `--with` list, raising on unknown targets."""
    if not with_.strip():
        return _REFRESH_TARGETS
    values = tuple(token.strip() for token in with_.split(",") if token.strip())
    unknown = [v for v in values if v not in _REFRESH_TARGETS]
    if unknown:
        raise click.BadParameter(
            f"unknown target(s) {unknown} - choose from {', '.join(_REFRESH_TARGETS)}"
        )
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
@click.pass_context
def refresh(ctx: click.Context, with_: str) -> None:
    """Ingest observations + campgrounds + land + dispersed + trails, then (re)build phenology."""
    cfg = ctx.obj["cfg"]
    targets = _parse_targets(with_)
    con = connect()
    override = load_location(con)
    if override is not None:
        cfg = cfg.model_copy(update={"home": Home(**override)})
    if "mushrooms" in targets:
        ingest(cfg, con)
    if "camps" in targets:
        ingest_campgrounds(cfg, con)
    if "land" in targets:
        ingest_public_land(cfg, con)
    if "dispersed" in targets:
        ingest_dispersed(cfg, con)
    if "trails" in targets:
        ingest_trails(cfg, con)
    if "mushrooms" in targets:
        build_phenology(con, cfg.cell_deg)
        region_count = (con.execute("SELECT count(*) FROM regions").fetchone() or (0,))[0]
        click.echo(
            f"Phenology rebuilt across {region_count} regions "
            f"({observation_count(con)} observations)."
        )
    else:
        click.echo(f"Warmed: {', '.join(targets)}.")
    con.close()


def _parse_months(months: str) -> list[int]:
    """Parse a comma-separated month list, raising a Click error on junk or out-of-range values."""
    try:
        values = [int(token) for token in months.split(",") if token.strip()]
    except ValueError as error:
        raise click.BadParameter(f"months must be integers 1-12: {months!r}") from error
    if not all(1 <= month <= 12 for month in values):
        raise click.BadParameter("months must be in 1-12")
    return values


@cli.command("plan")
@click.option(
    "--months", default="", help="Comma-separated months (1-12); default = current month."
)
@click.option(
    "--max-stops", default=5, type=click.IntRange(min=1), help="Maximum stays in the itinerary."
)
@click.option(
    "--max-drive-km",
    default=400.0,
    type=click.FloatRange(min=0, min_open=True),
    help="Max great-circle km per leg.",
)
@click.option("--any-camp", is_flag=True, help="Allow stops whose nearest camp isn't free-tagged.")
@click.pass_context
def plan_cmd(
    ctx: click.Context, months: str, max_stops: int, max_drive_km: float, any_camp: bool
) -> None:
    """Sequence the top destinations into a greedy, low-backtrack trip itinerary."""
    cfg = ctx.obj["cfg"]
    selected = _parse_months(months) or [dt.date.today().month]
    con = connect()
    trip = plan_route(
        con,
        months=selected,
        taxon_ids=cfg.taxon_ids,
        home_lat=cfg.home.lat,
        home_lng=cfg.home.lng,
        radius_km=cfg.home.radius_km,
        cell_deg=cfg.cell_deg,
        recent_weeks=cfg.recent_weeks,
        max_stops=max_stops,
        max_drive_km=max_drive_km,
        require_free_camp=not any_camp,
    )
    con.close()
    if not trip.stops:
        click.echo("No viable stops found - try a wider radius, more months, or --any-camp.")
        return
    click.echo(
        f"Trip from {cfg.home.name} - months {selected}, {trip.n_stops} stops, "
        f"{trip.total_drive_km:.0f} km total drive:"
    )
    for stop in trip.stops:
        top = ", ".join(hit.common_name for hit in stop.species[:3]) or "-"
        camp = (
            "no camp"
            if stop.camp is None
            else (
                f"{'FREE ' if stop.camp_is_free else ''}{stop.camp.name} "
                f"({stop.camp.distance_km:.0f} km)"
            )
        )
        click.echo(
            f"  {stop.order}. {stop.region_id}  +{stop.drive_km_from_prev:.0f} km  "
            f"score {stop.score_norm:.2f}  {stop.n_species} spp [{top}]  camp: {camp}"
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
