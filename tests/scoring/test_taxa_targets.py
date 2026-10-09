"""Rank-agnostic targets (issue #464): expansion, species phenology, and what each target rank does
to the ranking - hand-built fixtures, no network."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from foray.cache import ALL_TARGETS, Targets, bump_taxa_version, expand_targets, upsert_taxa
from foray.scoring import alerts, build_phenology, place_calendar, precise_observations, rank_destinations
from foray.scoring._sql import genus_name_map

RES = 4
FUNGI, CLASS, ORDER = 47170, 50814, 47169
FAMILY_A, FAMILY_B = 47167, 48423
AMANITA, CANTHARELLUS = 47168, 47348
AMANITA_SECTION = 9001  # shares the genus's name
MUSCARIA, PANTHERINA = 48715, 48716
MUSCARIA_VAR = 9002

APR_LAT, APR_LNG = 47.6, -122.3  # Amanita country
OCT_LAT, OCT_LNG = 44.0, -121.0  # Cantharellus country


def _rank(con: psycopg.Connection, taxon_ids: list[int], **kwargs):
    return rank_destinations(
        con,
        months=kwargs.pop("months", [4, 10]),
        taxon_ids=taxon_ids,
        home_lat=46.0,
        home_lng=-121.6,
        radius_km=500,
        h3_resolution=RES,
        **kwargs,
    )


def _hits(ranked) -> dict[str, dict[str, int]]:
    """region label -> {taxon name: month_count}, regions told apart by which side of 45N they sit."""
    return {
        ("apr" if region.center_lat > 45.5 else "oct"): {hit.name: hit.month_count for hit in region.species}
        for region in ranked
    }


@pytest.fixture(autouse=True)
def _seed(con: psycopg.Connection) -> None:
    upsert_taxa(
        con,
        [
            {"taxon_id": FUNGI, "name": "Fungi", "rank": "kingdom"},
            {"taxon_id": CLASS, "name": "Agaricomycetes", "rank": "class", "ancestor_ids": [FUNGI]},
            {"taxon_id": ORDER, "name": "Agaricales", "rank": "order", "ancestor_ids": [FUNGI, CLASS]},
            {"taxon_id": FAMILY_A, "name": "Amanitaceae", "rank": "family", "ancestor_ids": [FUNGI, CLASS, ORDER]},
            {"taxon_id": FAMILY_B, "name": "Cantharellaceae", "rank": "family", "ancestor_ids": [FUNGI, CLASS, ORDER]},
            {
                "taxon_id": AMANITA,
                "name": "Amanita",
                "common_name": "Amanitas",
                "rank": "genus",
                "ancestor_ids": [FUNGI, CLASS, ORDER, FAMILY_A],
            },
            {
                "taxon_id": CANTHARELLUS,
                "name": "Cantharellus",
                "common_name": "Chanterelles",
                "rank": "genus",
                "ancestor_ids": [FUNGI, CLASS, ORDER, FAMILY_B],
            },
            {
                "taxon_id": AMANITA_SECTION,
                "name": "Amanita",
                "rank": "section",
                "ancestor_ids": [FUNGI, CLASS, ORDER, FAMILY_A, AMANITA],
            },
            {
                "taxon_id": MUSCARIA,
                "name": "Amanita muscaria",
                "rank": "species",
                "ancestor_ids": [FUNGI, CLASS, ORDER, FAMILY_A, AMANITA, AMANITA_SECTION],
            },
            {
                "taxon_id": PANTHERINA,
                "name": "Amanita pantherina",
                "rank": "species",
                "ancestor_ids": [FUNGI, CLASS, ORDER, FAMILY_A, AMANITA, AMANITA_SECTION],
            },
            {
                "taxon_id": MUSCARIA_VAR,
                "name": "Amanita muscaria var. alba",
                "rank": "variety",
                "ancestor_ids": [FUNGI, CLASS, ORDER, FAMILY_A, AMANITA, AMANITA_SECTION, MUSCARIA],
            },
        ],
    )
    today = dt.date.today()
    rows: list[tuple] = []
    obs_id = 1

    def add(count: int, genus: int, species: int | None, lat: float, lng: float, day: dt.date) -> None:
        nonlocal obs_id
        for _ in range(count):
            rows.append((obs_id, genus, species, lat, lng, day, day.month, "research", 10, False))
            obs_id += 1

    add(6, AMANITA, MUSCARIA, APR_LAT, APR_LNG, dt.date(2022, 4, 15))
    add(4, AMANITA, PANTHERINA, APR_LAT, APR_LNG, dt.date(2022, 4, 16))
    add(5, AMANITA, None, APR_LAT, APR_LNG, dt.date(2022, 4, 17))  # genus-only identifications
    add(7, CANTHARELLUS, None, OCT_LAT, OCT_LNG, dt.date(2022, 10, 15))
    add(2, AMANITA, PANTHERINA, APR_LAT, APR_LNG, today)  # fresh, precise: feeds alerts / pins
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, species_id, lat, lng, observed_on, month, quality_grade,"
            " positional_accuracy, obscured) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
    build_phenology(con, RES)


# --- expansion ----------------------------------------------------------------------------


def test_expand_targets_by_rank(con: psycopg.Connection) -> None:
    assert expand_targets(con, [], [FUNGI]) == ALL_TARGETS
    assert expand_targets(con, [AMANITA]) == Targets((AMANITA,), (), False)
    assert expand_targets(con, [MUSCARIA]) == Targets((), (MUSCARIA,), False)
    assert expand_targets(con, [FAMILY_A]) == Targets((AMANITA,), (), False)
    assert expand_targets(con, [ORDER]) == Targets((AMANITA, CANTHARELLUS), (), False)
    assert expand_targets(con, [CLASS]) == Targets((AMANITA, CANTHARELLUS), (), False)


def test_expand_targets_scope_root_covers_everything(con: psycopg.Connection) -> None:
    assert expand_targets(con, [FUNGI], [FUNGI]).covers_all
    assert expand_targets(con, [CANTHARELLUS, FUNGI], [FUNGI]).covers_all


def test_expand_targets_unknown_id_is_a_genus_as_before_the_catalog_existed(con: psycopg.Connection) -> None:
    assert expand_targets(con, [424242]) == Targets((424242,), (), False)


def test_expand_targets_drops_a_species_under_a_picked_genus_or_group(con: psycopg.Connection) -> None:
    assert expand_targets(con, [AMANITA, MUSCARIA]) == Targets((AMANITA,), (), False)
    assert expand_targets(con, [FAMILY_A, MUSCARIA, PANTHERINA]) == Targets((AMANITA,), (), False)
    assert expand_targets(con, [CANTHARELLUS, MUSCARIA]) == Targets((CANTHARELLUS,), (MUSCARIA,), False)


def test_expand_targets_cache_follows_the_taxa_version(con: psycopg.Connection) -> None:
    assert expand_targets(con, [FAMILY_A]).genus_ids == (AMANITA,)
    # A new genus lands under the family; the cached expansion must not hide it.
    upsert_taxa(con, [{"taxon_id": 77777, "name": "Newgenus", "rank": "genus", "ancestor_ids": [FUNGI, FAMILY_A]}])
    assert expand_targets(con, [FAMILY_A]).genus_ids == (AMANITA, 77777)
    con.execute("UPDATE taxa SET is_active = FALSE WHERE taxon_id = 77777")
    bump_taxa_version(con)
    assert expand_targets(con, [FAMILY_A]).genus_ids == (AMANITA,)


# --- phenology ----------------------------------------------------------------------------


def test_species_phenology_plus_genus_only_rows_equal_the_genus_total(con: psycopg.Connection) -> None:
    """Rollup invariant: a genus's total = its species rows + its genus-only rows, and the species
    table never holds a genus-only observation."""
    genus_total = con.execute("SELECT sum(cnt) FROM phenology WHERE taxon_id = %s", [AMANITA]).fetchone()
    species_total = con.execute(
        "SELECT sum(cnt) FROM phenology_species WHERE taxon_id = ANY(%s)", [[MUSCARIA, PANTHERINA]]
    ).fetchone()
    genus_only = con.execute(
        "SELECT count(*) FROM observations WHERE taxon_id = %s AND species_id IS NULL", [AMANITA]
    ).fetchone()
    assert genus_total == (17,)
    assert species_total == (12,)
    assert genus_only == (5,)
    assert species_total[0] + genus_only[0] == genus_total[0]
    assert con.execute("SELECT count(*) FROM phenology_species WHERE taxon_id = %s", [AMANITA]).fetchone() == (0,)


# --- what a target rank does to the ranking ----------------------------------------------


def test_species_target_counts_only_that_species_never_genus_only_rows(con: psycopg.Connection) -> None:
    ranked = _rank(con, [MUSCARIA])
    assert _hits(ranked) == {"apr": {"Amanita muscaria": 6}}  # not the 5 genus-only, not pantherina


def test_species_target_wears_its_genus_icon(con: psycopg.Connection) -> None:
    (region,) = _rank(con, [MUSCARIA])
    genus_icon = genus_name_map(con, [AMANITA])[AMANITA].icon
    assert region.species[0].icon == genus_icon
    assert region.species[0].taxon_id == MUSCARIA


def test_genus_target_counts_species_and_genus_only_rows(con: psycopg.Connection) -> None:
    assert _hits(_rank(con, [AMANITA])) == {"apr": {"Amanita": 17}}


@pytest.mark.parametrize("target", [FAMILY_A, ORDER, CLASS])
def test_group_targets_sum_their_descendant_genera(con: psycopg.Connection, target: int) -> None:
    hits = _hits(_rank(con, [target]))
    assert hits["apr"] == {"Amanita": 17}
    assert ("oct" in hits) == (target != FAMILY_A)  # the family holds only Amanita
    if target != FAMILY_A:
        assert hits["oct"] == {"Cantharellus": 7}


def test_a_target_at_any_rank_changes_the_ranking(con: psycopg.Connection) -> None:
    by_species = _rank(con, [MUSCARIA])
    by_other_family = _rank(con, [FAMILY_B])
    assert [region.center_lat > 45.5 for region in by_species] == [True]
    assert [region.center_lat > 45.5 for region in by_other_family] == [False]


def test_mixed_genus_and_species_targets_are_unioned(con: psycopg.Connection) -> None:
    hits = _hits(_rank(con, [CANTHARELLUS, PANTHERINA]))
    assert hits == {"apr": {"Amanita pantherina": 6}, "oct": {"Cantharellus": 7}}


def test_scope_root_target_means_no_filter(con: psycopg.Connection) -> None:
    hits = _hits(_rank(con, [FUNGI]))
    assert set(hits["apr"]) == {"Amanita"} and set(hits["oct"]) == {"Cantharellus"}


def test_ranking_cache_distinguishes_species_from_genus_targets(con: psycopg.Connection) -> None:
    assert _hits(_rank(con, [MUSCARIA]))["apr"] == {"Amanita muscaria": 6}
    assert _hits(_rank(con, [AMANITA]))["apr"] == {"Amanita": 17}


# --- the observation-level reads ----------------------------------------------------------


def test_calendar_for_a_species_target(con: psycopg.Connection) -> None:
    (region,) = _rank(con, [MUSCARIA])
    calendar = place_calendar(con, region_id=region.region_id, taxon_ids=[MUSCARIA])
    assert calendar[4]["total"] == 6
    assert list(calendar[4]["species"]) == ["Amanita muscaria"]
    assert calendar[10]["total"] == 0


def test_alerts_name_the_species_when_a_species_is_targeted(con: psycopg.Connection) -> None:
    def fresh(taxon_ids: list[int]) -> list[dict]:
        return alerts(con, taxon_ids=taxon_ids, home_lat=46.0, home_lng=-121.6, radius_km=500, h3_resolution=RES)

    by_species = fresh([PANTHERINA])
    assert [(species["name"], species["count"]) for region in by_species for species in region["species"]] == [
        ("Amanita pantherina", 2)
    ]
    by_genus = fresh([AMANITA])
    assert [species["name"] for region in by_genus for species in region["species"]] == ["Amanita"]
    assert fresh([MUSCARIA]) == []  # no fresh muscaria


def test_precise_observations_filter_by_species(con: psycopg.Connection) -> None:
    def pins(taxon_ids: list[int]) -> int:
        return len(
            precise_observations(
                con, taxon_ids=taxon_ids, lat=APR_LAT, lng=APR_LNG, radius_km=50, months=list(range(1, 13))
            )
        )

    assert pins([PANTHERINA]) == 6
    assert pins([MUSCARIA]) == 6
    assert pins([AMANITA]) == 17
    assert pins([CANTHARELLUS]) == 0
