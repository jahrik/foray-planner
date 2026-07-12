// Domain shapes for the JSON API. The backend endpoints return loosely-typed dicts
// (see src/foray/scoring.py), so the generated schema (./schema.ts) only pins the
// request/response envelope. These interfaces mirror the actual field-level shapes we
// consume and are the single place to update if a payload changes.

export interface Home {
  name: string;
  lat: number;
  lng: number;
  radius_km: number;
}

export interface Config {
  home: Home;
  cell_deg: number;
  recent_weeks: number;
  refreshing: boolean;
  last_error: string | null;
}

export interface Species {
  taxon_id: number;
  common_name: string;
  inat_url: string;
}

/** A target-species contribution to a ranked region. */
export interface SpeciesHit {
  taxon_id: number;
  common_name: string;
  month_count: number;
  total_count: number;
  w_pheno: number;
}

/** One ranked destination region (`GET /api/destinations`). */
export interface RegionScore {
  region_id: string;
  center_lat: number;
  center_lng: number;
  distance_km: number;
  score: number;
  score_norm: number;
  n_species: number;
  recent_count: number;
  species: SpeciesHit[];
}

/** A month bucket in the place calendar (`GET /api/calendar`). */
export interface CalendarBucket {
  total: number;
  species: Record<string, number>;
}

/** Keyed by month number 1-12. */
export type Calendar = Record<number, CalendarBucket>;

/** A recently-observed species within an alert region. */
export interface AlertHit {
  taxon_id: number;
  common_name: string;
  count: number;
  last_seen: string;
}

/** A region with recent activity (`GET /api/alerts`). */
export interface AlertRegion {
  region_id: string;
  center_lat: number;
  center_lng: number;
  distance_km: number;
  total: number;
  species: AlertHit[];
}

/** A campsite near a region (`GET /api/camps`). `free` is null when the source is silent. */
export interface CampSite {
  id: string;
  name: string;
  kind: string;
  fee: string | null;
  free: boolean | null;
  center_lat: number;
  center_lng: number;
  distance_km: number;
  source: string;
  url: string;
}

/** A public-land ownership polygon (`GET /api/land`). `geometry` is raw GeoJSON. */
export interface LandUnit {
  id: string;
  agency: string;
  unit: string;
  source: string;
  url: string;
  geometry: GeoJSON.Geometry;
}

/** A trail near a hotspot (`GET /api/trails`). `geometry` is raw GeoJSON (line or point). */
export interface Trail {
  id: string;
  name: string;
  kind: string; // "path" | "route" | "trailhead"
  source: string;
  url: string;
  center_lat: number;
  center_lng: number;
  distance_km: number;
  camp_distance_km: number | null; // nearest campsite, null when none cached
  geometry: GeoJSON.Geometry;
}

/** One week-long stay in a planned trip (a `TripPlan.stops` entry). */
export interface Stop {
  order: number;
  region_id: string;
  center_lat: number;
  center_lng: number;
  score_norm: number;
  n_species: number;
  recent_count: number;
  species: SpeciesHit[];
  drive_km_from_prev: number;
  cumulative_drive_km: number;
  camp: CampSite | null; // nearest camp, null when none is within range
  camp_is_free: boolean;
}

/** A greedy multi-stop itinerary (`GET /api/plan`). */
export interface TripPlan {
  home_lat: number;
  home_lng: number;
  months: number[];
  n_stops: number;
  total_drive_km: number;
  stops: Stop[];
  skipped_unreachable: number;
}

export interface LocationResponse {
  home: Home;
}

/** FastAPI's error envelope (`{ "detail": ... }`). */
export interface ApiError {
  detail?: string;
}
