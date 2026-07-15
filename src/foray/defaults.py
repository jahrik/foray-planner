"""Built-in defaults: home location, region granularity, species, and coverage."""

from __future__ import annotations

HOME_LAT = 47.6062
HOME_LNG = -122.3321
HOME_RADIUS_KM = 150
CELL_DEG = 0.25

# Country-level iNat place_ids - one ingest_region() call per entry covers every sub-region
# within it in a single (paginated) query. Simpler and more correct than looping every state/
# province (no double-counting/missing observations near internal borders). Add more entries
# here as coverage expands to other countries; no code changes needed elsewhere.
COUNTRIES = [
    {"name": "United States", "place_id": 1},
]

SPECIES = [
    {"taxon_id": 47348, "name": "Cantharellus", "common_name": "Chanterelles", "rank": "genus"},
    {"taxon_id": 48611, "name": "Craterellus", "common_name": "Black Trumpets", "rank": "genus"},
    {"taxon_id": 56830, "name": "Morchella", "common_name": "Morels", "rank": "genus"},
    {
        "taxon_id": 48703,
        "name": "Boletus",
        "common_name": "King Boletes & Porcini",
        "rank": "genus",
    },
    {"taxon_id": 53490, "name": "Suillus", "common_name": "Slippery Jacks", "rank": "genus"},
    {"taxon_id": 54203, "name": "Leccinum", "common_name": "Scaber Stalks", "rank": "genus"},
    {"taxon_id": 48422, "name": "Hydnum", "common_name": "Hedgehogs", "rank": "genus"},
    {"taxon_id": 49160, "name": "Hericium", "common_name": "Lion's Mane", "rank": "genus"},
    {
        "taxon_id": 48431,
        "name": "Laetiporus",
        "common_name": "Chicken of the Woods",
        "rank": "genus",
    },
    {"taxon_id": 53716, "name": "Grifola", "common_name": "Hen of the Woods", "rank": "genus"},
    {
        "taxon_id": 63020,
        "name": "Sparassis",
        "common_name": "Cauliflower Mushrooms",
        "rank": "genus",
    },
    {"taxon_id": 48496, "name": "Pleurotus", "common_name": "Oyster Mushrooms", "rank": "genus"},
    {
        "taxon_id": 62484,
        "name": "Tricholoma",
        "common_name": "Matsutake & Tricholomas",
        "rank": "genus",
    },
    {"taxon_id": 55591, "name": "Lepista", "common_name": "Blewits", "rank": "genus"},
    {"taxon_id": 54597, "name": "Lactarius", "common_name": "Milk Caps", "rank": "genus"},
    {
        "taxon_id": 49548,
        "name": "Agaricus",
        "common_name": "Field & Button Mushrooms",
        "rank": "genus",
    },
    {"taxon_id": 47393, "name": "Coprinus", "common_name": "Shaggy Manes", "rank": "genus"},
    {"taxon_id": 57693, "name": "Calvatia", "common_name": "Giant Puffballs", "rank": "genus"},
    {"taxon_id": 48444, "name": "Lycoperdon", "common_name": "Puffballs", "rank": "genus"},
    {"taxon_id": 48246, "name": "Hypomyces", "common_name": "Lobster Mushroom", "rank": "genus"},
    {"taxon_id": 50817, "name": "Auricularia", "common_name": "Wood Ears", "rank": "genus"},
]

# All 50 US states, keyed by iNat place_id (admin_level=10, place_type=8 - state boundary)
# and a (west, south, east, north) bbox used by the land/trails per-region ingest. Alaska's
# bbox is clamped to -180..-130 to avoid an antimeridian-crossing rectangle (its true polygon
# dips just past +179 near the far-west Aleutians) - those islands are dropped rather than
# mishandled.
COVERAGE = [
    {"name": "Alabama", "place_id": 19, "bbox": (-88.4732, 30.1447, -84.8882, 35.008)},
    {"name": "Alaska", "place_id": 6, "bbox": (-180.0, 51.0, -130.0, 71.5)},
    {"name": "Arizona", "place_id": 40, "bbox": (-114.814, 31.3322, -109.0452, 37.0002)},
    {"name": "Arkansas", "place_id": 36, "bbox": (-94.6178, 33.0041, -89.6444, 36.4995)},
    {"name": "California", "place_id": 14, "bbox": (-124.4805, 32.5288, -114.1312, 41.9983)},
    {"name": "Colorado", "place_id": 34, "bbox": (-109.0509, 36.993, -102.0421, 41.0024)},
    {"name": "Connecticut", "place_id": 49, "bbox": (-73.7278, 40.9509, -71.7872, 42.0496)},
    {"name": "Delaware", "place_id": 4, "bbox": (-75.7886, 38.4516, -74.9863, 39.8348)},
    {"name": "Florida", "place_id": 21, "bbox": (-87.6347, 24.3969, -79.9754, 31.0007)},
    {"name": "Georgia", "place_id": 23, "bbox": (-85.6052, 30.3558, -80.7514, 34.9973)},
    {"name": "Hawaii", "place_id": 11, "bbox": (-178.4332, 18.8668, -154.7579, 28.5101)},
    {"name": "Idaho", "place_id": 22, "bbox": (-117.2429, 41.9999, -111.0467, 49.0008)},
    {"name": "Illinois", "place_id": 35, "bbox": (-91.513, 36.9735, -87.0199, 42.5085)},
    {"name": "Indiana", "place_id": 20, "bbox": (-88.0978, 37.7736, -84.7853, 41.7602)},
    {"name": "Iowa", "place_id": 24, "bbox": (-96.6365, 40.3825, -90.1406, 43.5005)},
    {"name": "Kansas", "place_id": 25, "bbox": (-102.0517, 36.993, -94.5887, 40.0031)},
    {"name": "Kentucky", "place_id": 26, "bbox": (-89.571, 36.4971, -81.9648, 39.1477)},
    {"name": "Louisiana", "place_id": 27, "bbox": (-94.043, 28.8606, -88.7673, 33.0192)},
    {"name": "Maine", "place_id": 17, "bbox": (-71.0839, 42.9171, -66.8854, 47.4599)},
    {"name": "Maryland", "place_id": 39, "bbox": (-79.4877, 37.8866, -74.9863, 39.7222)},
    {"name": "Massachusetts", "place_id": 2, "bbox": (-73.5081, 41.1898, -69.8665, 42.8866)},
    {"name": "Michigan", "place_id": 29, "bbox": (-90.4184, 41.6961, -82.123, 48.3061)},
    {"name": "Minnesota", "place_id": 38, "bbox": (-97.234, 43.5004, -89.4834, 49.3844)},
    {"name": "Mississippi", "place_id": 37, "bbox": (-91.654, 30.1398, -88.0982, 34.9957)},
    {"name": "Missouri", "place_id": 28, "bbox": (-95.7685, 35.9957, -89.1025, 40.6136)},
    {"name": "Montana", "place_id": 16, "bbox": (-116.0491, 44.3592, -104.0397, 49.0008)},
    {"name": "Nebraska", "place_id": 3, "bbox": (-104.0532, 40.0, -95.3083, 43.0006)},
    {"name": "Nevada", "place_id": 50, "bbox": (-120.001, 35.0019, -114.0415, 41.9948)},
    {"name": "New Hampshire", "place_id": 41, "bbox": (-72.5555, 42.697, -70.5751, 45.3055)},
    {"name": "New Jersey", "place_id": 51, "bbox": (-75.5636, 38.7887, -73.8867, 41.3574)},
    {"name": "New Mexico", "place_id": 9, "bbox": (-109.05, 31.3322, -103.0022, 37.0001)},
    {"name": "New York", "place_id": 48, "bbox": (-79.7624, 40.4774, -71.7775, 45.0159)},
    {"name": "North Carolina", "place_id": 30, "bbox": (-84.3219, 33.7529, -75.4008, 36.5882)},
    {"name": "North Dakota", "place_id": 13, "bbox": (-104.0491, 45.9405, -96.5544, 49.0006)},
    {"name": "Ohio", "place_id": 31, "bbox": (-84.8202, 38.41, -80.519, 42.3271)},
    {"name": "Oklahoma", "place_id": 12, "bbox": (-103.0024, 33.6275, -94.431, 37.0001)},
    {"name": "Oregon", "place_id": 10, "bbox": (-124.7035, 41.9999, -116.4634, 46.2991)},
    {"name": "Pennsylvania", "place_id": 42, "bbox": (-80.5199, 39.7214, -74.6895, 42.5161)},
    {"name": "Rhode Island", "place_id": 8, "bbox": (-71.9073, 41.0963, -71.0886, 42.0188)},
    {"name": "South Carolina", "place_id": 43, "bbox": (-83.3536, 32.0335, -78.4993, 35.2154)},
    {"name": "South Dakota", "place_id": 44, "bbox": (-104.053, 42.4827, -96.4365, 45.9453)},
    {"name": "Tennessee", "place_id": 45, "bbox": (-90.3101, 34.9884, -81.6469, 36.6783)},
    {"name": "Texas", "place_id": 18, "bbox": (-106.6456, 25.8378, -93.5081, 36.5003)},
    {"name": "Utah", "place_id": 52, "bbox": (-114.0501, 36.999, -109.0452, 42.0017)},
    {"name": "Vermont", "place_id": 47, "bbox": (-73.4379, 42.7268, -71.465, 45.0136)},
    {"name": "Virginia", "place_id": 7, "bbox": (-83.6754, 36.5427, -75.1664, 39.466)},
    {"name": "Washington", "place_id": 46, "bbox": (-124.8485, 45.5438, -116.9156, 49.002)},
    {"name": "West Virginia", "place_id": 33, "bbox": (-82.6445, 37.2015, -77.7195, 40.6388)},
    {"name": "Wisconsin", "place_id": 32, "bbox": (-92.8871, 42.4935, -86.2495, 47.3098)},
    {"name": "Wyoming", "place_id": 15, "bbox": (-111.0546, 40.9979, -104.0532, 45.001)},
]
