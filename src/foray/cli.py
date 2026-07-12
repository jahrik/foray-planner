"""`foray` command-line entry point."""

from __future__ import annotations

import click

from foray.cache import connect, observation_count
from foray.camps import ingest_campgrounds
from foray.config import load_config
from foray.ingest import ingest
from foray.scoring import build_phenology


@click.group()
@click.option("--config", "config_path", default="config.yaml", help="Path to config.yaml")
@click.pass_context
def cli(ctx: click.Context, config_path: str) -> None:
    """Plan mushroom-hunting trips from iNaturalist phenology."""
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


@cli.command()
@click.pass_context
def refresh(ctx: click.Context) -> None:
    """Ingest observations + campgrounds, then (re)build the phenology + regions tables."""
    cfg = ctx.obj["cfg"]
    con = connect(cfg.db_path)
    ingest(cfg, con)
    ingest_campgrounds(cfg, con)
    build_phenology(con, cfg.cell_deg)
    region_count = (con.execute("SELECT count(*) FROM regions").fetchone() or (0,))[0]
    click.echo(
        f"Phenology rebuilt across {region_count} regions ({observation_count(con)} observations)."
    )
    con.close()


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
