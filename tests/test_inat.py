"""foray.inat pagination tests - no network, get_taxa mocked (see test_ingest_region.py)."""

from __future__ import annotations

from unittest.mock import patch

from foray.inat import FUNGI_TAXON_ID, iter_fungi_genera


def _page(ids: list[int]) -> dict:
    return {"results": [{"id": taxon_id, "name": f"Genus{taxon_id}", "rank": "genus"} for taxon_id in ids]}


def test_iter_fungi_genera_walks_id_above_pages() -> None:
    with patch("foray.inat.get_taxa") as mock_get_taxa, patch("foray.inat._PAGE_SIZE", 2):
        mock_get_taxa.side_effect = [
            _page([1, 2]),
            _page([3]),
        ]
        results = list(iter_fungi_genera())

    assert [r["id"] for r in results] == [1, 2, 3]
    assert mock_get_taxa.call_count == 2
    first_kwargs = mock_get_taxa.call_args_list[0].kwargs
    assert first_kwargs["taxon_id"] == FUNGI_TAXON_ID
    assert first_kwargs["rank"] == "genus"
    assert first_kwargs["id_above"] == 0
    second_kwargs = mock_get_taxa.call_args_list[1].kwargs
    assert second_kwargs["id_above"] == 2


def test_iter_fungi_genera_empty_result_stops_immediately() -> None:
    with patch("foray.inat.get_taxa") as mock_get_taxa:
        mock_get_taxa.return_value = {"results": []}
        results = list(iter_fungi_genera())

    assert results == []
    mock_get_taxa.assert_called_once()
