"""Satellite tile-stitching tests - no network (mocked Esri tile transport)."""

from __future__ import annotations

from io import BytesIO

import httpx
import pytest
from PIL import Image

from foray.geo import web_mercator_bbox_m
from foray.sources.satellite import (
    TILE_PX,
    _meters_to_global_pixel,
    _zoom_for_diameter,
    fetch_region_satellite,
)

HOOD_LAT, HOOD_LNG = 45.3066, -121.7797


def test_zoom_for_diameter_shrinks_as_target_px_grows() -> None:
    # A bigger target raster for the same ground disk needs more, smaller-ground-footprint
    # tiles - i.e. a higher zoom.
    assert _zoom_for_diameter(HOOD_LAT, 20_000, 1024) < _zoom_for_diameter(HOOD_LAT, 20_000, 4096)


def test_zoom_for_diameter_shrinks_as_ground_disk_grows() -> None:
    # A bigger ground disk at the same target raster size needs fewer, bigger-ground-footprint
    # tiles - i.e. a lower zoom.
    assert _zoom_for_diameter(HOOD_LAT, 80_000, 2048) < _zoom_for_diameter(HOOD_LAT, 10_000, 2048)


def test_zoom_for_diameter_is_clamped_to_valid_tile_zooms() -> None:
    assert _zoom_for_diameter(HOOD_LAT, 1, 100_000) <= 19
    assert _zoom_for_diameter(HOOD_LAT, 100_000_000, 1) >= 0


def test_meters_to_global_pixel_round_trips_the_top_left_of_the_world() -> None:
    # (-R*pi, +R*pi) is the Web Mercator NW corner of the whole pyramid -> global pixel (0, 0).
    import math

    r = 6378137.0
    px, py = _meters_to_global_pixel(-math.pi * r, math.pi * r, zoom=5)
    assert px == pytest.approx(0.0, abs=1e-6)
    assert py == pytest.approx(0.0, abs=1e-6)


def _solid_tile_response(color: tuple[int, int, int, int]) -> httpx.Response:
    tile = Image.new("RGBA", (TILE_PX, TILE_PX), color)
    buf = BytesIO()
    tile.save(buf, format="PNG")
    return httpx.Response(200, content=buf.getvalue())


def test_fetch_region_satellite_stitches_and_crops_to_the_true_bbox() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _solid_tile_response((10, 20, 30, 255))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    radius_m = 15_000.0
    image_bytes, labels_bytes = fetch_region_satellite(HOOD_LAT, HOOD_LNG, radius_m, client=client)

    image = Image.open(BytesIO(image_bytes))
    labels = Image.open(BytesIO(labels_bytes))
    assert image.format == "JPEG"
    assert labels.format == "PNG"

    # Both layers are cropped from the same bbox at the same zoom, so they must match exactly -
    # a size mismatch here would mean the image and its labels overlay misalign on screen.
    assert image.size == labels.size

    # Cropped to (approximately) the requested disk's diameter, not left at a raw tile-grid
    # multiple of 256px.
    bbox = web_mercator_bbox_m(HOOD_LAT, HOOD_LNG, radius_m)
    zoom = _zoom_for_diameter(HOOD_LAT, radius_m * 2, 2048)
    left, top = _meters_to_global_pixel(bbox[0], bbox[3], zoom)
    right, bottom = _meters_to_global_pixel(bbox[2], bbox[1], zoom)
    assert image.size[0] == pytest.approx(right - left, abs=1)
    assert image.size[1] == pytest.approx(bottom - top, abs=1)


def test_fetch_region_satellite_propagates_a_failing_tile_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPError):
        fetch_region_satellite(HOOD_LAT, HOOD_LNG, 15_000.0, client=client)
