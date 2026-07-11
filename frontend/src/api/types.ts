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

export interface LocationResponse {
  home: Home;
}

/** FastAPI's error envelope (`{ "detail": ... }`). */
export interface ApiError {
  detail?: string;
}
