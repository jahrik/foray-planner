"""``foray.refresh.run_home_refresh`` - the home-radius ingest sequence shared by the CLI
(``foray refresh``) and the API (``POST /api/refresh``). The individual ingests have their
own tests; here we only pin the orchestration: which layers run, in what order, how the
progress bar is sliced, and that cancellation short-circuits it."""

from __future__ import annotations

import threading

import psycopg
import pytest

from foray.config import Home, Settings
from foray.refresh import REFRESH_LAYERS, run_home_refresh

_CFG = Settings(home=Home(name="Home", lat=47.6, lng=-122.3, radius_km=50.0))


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub every ingest ``run_home_refresh`` can call, recording the call order."""
    seen: list[str] = []
    for dotted, label in (
        ("foray.refresh.ingest", "mushrooms"),
        ("foray.camps.ingest_campgrounds", "camps"),
        ("foray.land.ingest_public_land", "land"),
        ("foray.dispersed.ingest_dispersed", "dispersed"),
        ("foray.trails.ingest_trails", "trails"),
        ("foray.scoring.build_phenology", "phenology"),
    ):
        monkeypatch.setattr(dotted, lambda *args, _label=label, **kwargs: seen.append(_label))
    return seen


def test_runs_every_layer_in_ingest_order(con: psycopg.Connection, recorder: list[str]) -> None:
    run_home_refresh(_CFG, con, REFRESH_LAYERS)
    assert recorder == ["mushrooms", "camps", "land", "dispersed", "trails", "phenology"]


def test_subset_skips_other_layers_and_phenology(con: psycopg.Connection, recorder: list[str]) -> None:
    run_home_refresh(_CFG, con, ("camps", "trails"))
    assert recorder == ["camps", "trails"]


def test_mushrooms_alone_still_rebuilds_phenology(con: psycopg.Connection, recorder: list[str]) -> None:
    run_home_refresh(_CFG, con, ("mushrooms",))
    assert recorder == ["mushrooms", "phenology"]


def test_progress_callback_emits_the_phenology_handoff(con: psycopg.Connection, recorder: list[str]) -> None:
    updates: list[tuple[str, float]] = []
    run_home_refresh(_CFG, con, REFRESH_LAYERS, progress_cb=lambda step, pct: updates.append((step, pct)))
    # Only the phenology handoff is emitted directly by run_home_refresh; the per-layer
    # ingests emit through the sliced callback, which the stubs above don't drive.
    assert updates == [("Building phenology…", 90.0)]


def test_abort_event_stops_before_the_next_phase(
    con: psycopg.Connection, recorder: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    abort = threading.Event()

    def abort_during_camps(*args: object, **kwargs: object) -> None:
        recorder.append("camps")
        abort.set()

    monkeypatch.setattr("foray.camps.ingest_campgrounds", abort_during_camps)
    run_home_refresh(_CFG, con, REFRESH_LAYERS, abort_event=abort)
    assert recorder == ["mushrooms", "camps"]
