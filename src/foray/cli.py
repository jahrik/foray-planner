"""`foray` command-line entry point."""

from __future__ import annotations

import datetime as dt
import logging

import click

from foray.cache import connect, observation_count
from foray.camps import ingest_campgrounds
from foray.config import load_config
from foray.dispersed import ingest_dispersed
from foray.ingest import ingest
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
@click.option("--config", "config_path", default="config.yaml", help="Path to config.yaml")
@click.pass_context
def cli(ctx: click.Context, config_path: str) -> None:
    """Plan mushroom-hunting trips from iNaturalist phenology."""
    _setup_logging()
    ctx.ensure_object(dict)
    ctx.obj["cfg"] = load_config(config_path)


@cli.command("ingest")
@click.pass_context
def ingest_cmd(ctx: click.Context) -> None:
    """Pull observations for all seed species within the home radius into the cache."""
    cfg = ctx.obj["cfg"]
    con = connect(cfg.db_path)
    click.echo(f"Ingesting {len(cfg.species)} species within {cfg.home.radius_km} km of home…")
    counts = ingest(cfg, con)
    for species in cfg.species:
        click.echo(f"  {species.common_name:28s} {counts.get(species.taxon_id, 0):>6d}")
    click.echo(f"Total observations cached: {observation_count(con)}")
    con.close()


@cli.command("camps")
@click.pass_context
def camps_cmd(ctx: click.Context) -> None:
    """Ingest developed campgrounds (Recreation.gov RIDB) within the home radius."""
    cfg = ctx.obj["cfg"]
    con = connect(cfg.db_path)
    count = ingest_campgrounds(cfg, con)
    if count:
        click.echo(f"Cached {count} campgrounds within {cfg.home.radius_km} km of home.")
    else:
        click.echo("No campgrounds ingested — set RIDB_API_KEY to enable camp data.")
    con.close()


@cli.command("land")
@click.pass_context
def land_cmd(ctx: click.Context) -> None:
    """Ingest public-land ownership (BLM + USFS) polygons within the home radius."""
    cfg = ctx.obj["cfg"]
    con = connect(cfg.db_path)
    count = ingest_public_land(cfg, con)
    click.echo(f"Cached {count} public-land units within {cfg.home.radius_km} km of home.")
    con.close()


@cli.command("dispersed")
@click.pass_context
def dispersed_cmd(ctx: click.Context) -> None:
    """Ingest OSM dispersed camping (reported sites + road∩public-land proxy) near home."""
    cfg = ctx.obj["cfg"]
    con = connect(cfg.db_path)
    count = ingest_dispersed(cfg, con)
    click.echo(f"Cached {count} dispersed/reported sites within {cfg.home.radius_km} km of home.")
    con.close()


@cli.command("trails")
@click.pass_context
def trails_cmd(ctx: click.Context) -> None:
    """Ingest OSM trails (paths, hiking routes, trailheads) near home."""
    cfg = ctx.obj["cfg"]
    con = connect(cfg.db_path)
    count = ingest_trails(cfg, con)
    click.echo(f"Cached {count} trails within {cfg.home.radius_km} km of home.")
    con.close()


@cli.command()
@click.pass_context
def refresh(ctx: click.Context) -> None:
    """Ingest observations + campgrounds + land + dispersed + trails, then (re)build phenology."""
    cfg = ctx.obj["cfg"]
    con = connect(cfg.db_path)
    ingest(cfg, con)
    ingest_campgrounds(cfg, con)
    ingest_public_land(cfg, con)
    ingest_dispersed(cfg, con)
    ingest_trails(cfg, con)
    build_phenology(con, cfg.cell_deg)
    region_count = (con.execute("SELECT count(*) FROM regions").fetchone() or (0,))[0]
    click.echo(
        f"Phenology rebuilt across {region_count} regions ({observation_count(con)} observations)."
    )
    con.close()


@cli.command("plan")
@click.option(
    "--months", default="", help="Comma-separated months (1-12); default = current month."
)
@click.option("--max-stops", default=5, type=int, help="Maximum stays in the itinerary.")
@click.option("--max-drive-km", default=400.0, type=float, help="Max great-circle km per leg.")
@click.option("--any-camp", is_flag=True, help="Allow stops whose nearest camp isn't free-tagged.")
@click.pass_context
def plan_cmd(
    ctx: click.Context, months: str, max_stops: int, max_drive_km: float, any_camp: bool
) -> None:
    """Sequence the top destinations into a greedy, low-backtrack trip itinerary."""
    cfg = ctx.obj["cfg"]
    selected = [int(t) for t in months.split(",") if t.strip()] or [dt.date.today().month]
    con = connect(cfg.db_path)
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
        click.echo("No viable stops found — try a wider radius, more months, or --any-camp.")
        return
    click.echo(
        f"Trip from {cfg.home.name} — months {selected}, {trip.n_stops} stops, "
        f"{trip.total_drive_km:.0f} km total drive:"
    )
    for stop in trip.stops:
        top = ", ".join(hit.common_name for hit in stop.species[:3]) or "—"
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
        click.echo(f"  ({trip.skipped_unreachable} viable stop(s) skipped — beyond max drive.)")


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
