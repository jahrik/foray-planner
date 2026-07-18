// Domain shapes for the JSON API, re-exported from the generated schema (./schema.ts, built by
// `npm run gen:api` from the backend's OpenAPI spec). The backend now declares real Pydantic
// response models for every route (see src/foray/api_models.py), so these are thin aliases onto
// `components["schemas"][...]` rather than hand-maintained duplicates - a backend field rename
// shows up here (and at every import site) as a compile error instead of silently drifting.

import type { components } from "./schema";

export type Home = components["schemas"]["Home"];
export type Config = components["schemas"]["ConfigResponse"];
export type Species = components["schemas"]["SpeciesResponse"];

/** A target-species contribution to a ranked region. */
export type SpeciesHit = components["schemas"]["SpeciesHit"];

/** One ranked destination region (`GET /api/destinations`). */
export type RegionScore = components["schemas"]["RegionScore"];

/** A month bucket in the place calendar (`GET /api/calendar`). */
export type CalendarBucket = components["schemas"]["CalendarBucket"];

/** Keyed by month number 1-12 (JSON always stringifies dict keys). */
export type Calendar = Record<string, CalendarBucket>;

/** A recently-observed species within an alert region. */
export type AlertHit = components["schemas"]["AlertHit"];

/** A region with recent activity (`GET /api/alerts`). */
export type AlertRegion = components["schemas"]["AlertRegion"];

/** A campsite near a region (`GET /api/camps`). `free` is null when the source is silent. */
export type CampSite = components["schemas"]["CampSite"];

/** A public-land ownership polygon (`GET /api/land`). `geometry` is raw GeoJSON. */
export type LandUnit = Omit<components["schemas"]["LandUnit"], "geometry"> & {
  geometry: GeoJSON.Geometry;
};

/** A trail near a hotspot (`GET /api/trails`). `geometry` is raw GeoJSON (line or point). */
export type Trail = Omit<components["schemas"]["Trail"], "geometry"> & {
  geometry: GeoJSON.Geometry;
};

/** One week-long stay in a planned trip (a `TripPlan.stops` entry). */
export type Stop = components["schemas"]["Stop"];

/** A greedy multi-stop itinerary (`GET /api/plan`). */
export type TripPlan = components["schemas"]["TripPlan"];

export type LocationResponse = components["schemas"]["LocationResponse"];

/** A configured coverage region with ingest freshness info (`GET /api/coverage`). */
export type CoverageRegion = components["schemas"]["CoverageRegionResponse"];

/** A displayable observation photo (`GET /api/observations/photos`) - already license-filtered. */
export type ObservationPhoto = components["schemas"]["ObservationPhoto"];

/** A recent observation, with any eligible thumbnails (`GET /api/observations/photos`). */
export type RecentObservation = components["schemas"]["RecentObservation"];

/** FastAPI's error envelope (`{ "detail": ... }`). */
export interface ApiError {
  detail?: string;
}
