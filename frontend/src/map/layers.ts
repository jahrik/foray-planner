import L from "leaflet";

import { getJson } from "../api/client";
import type { CampSite, FireNear, LandUnit, PreciseObservation, Trail, TrailPath } from "../api/types";
import { createRunGuard } from "../ui/card-select";
import { feeLabel } from "../format";
import { circleStyle } from "./markers";
import { buildPopup } from "./popup";
import {
  addCampMarker,
  addPreciseMarker,
  CAMP_FREE,
  CAMP_OSM,
  CAMP_PAID,
  clearCamps,
  clearFire,
  clearLand,
  clearPrecise,
  clearSelectedTrail,
  FIRE_ACTIVE,
  FIRE_SCAR,
  HOME_RING,
  LAND_COLORS,
  LAND_DEFAULT,
  map,
  PRECISE,
  regionRadiusKm,
  renderLegend,
  setFireLayer,
  setFocused,
  setLandLayer,
  setSelectedTrail,
  TRAIL,
} from "./map";
import { dist, displayName, errorDetail, monthsParam, qs, setStatus, state } from "../state";

export const campsOn = (): boolean => qs<HTMLInputElement>("#show-camps").checked;
export const dispersedOn = (): boolean => qs<HTMLInputElement>("#show-dispersed").checked;
export const freeOnly = (): boolean => qs<HTMLInputElement>("#free-camps").checked;
export const blmOn = (): boolean => qs<HTMLInputElement>("#show-land-blm").checked;
export const usfsOn = (): boolean => qs<HTMLInputElement>("#show-land-usfs").checked;
export const tribalOn = (): boolean => qs<HTMLInputElement>("#show-land-tribal").checked;
const LAND_TOGGLES: Record<string, () => boolean> = { BLM: blmOn, USFS: usfsOn, Tribal: tribalOn };
const landOn = (): boolean => blmOn() || usfsOn() || tribalOn();

// OSM dispersed-camping layer: sites tagged campable in OpenStreetMap (kind='reported').
const isDispersed = (site: CampSite): boolean => site.kind === "reported";

// Fetch + plot camping near the focused region. `/api/camps` returns developed campgrounds and
// the OSM dispersed layer together; each is drawn only when its toggle is on. No-op (just clears)
// when neither is on. Failures degrade quietly to a status line rather than throwing.
export async function loadCamps(): Promise<void> {
  clearCamps();
  renderLegend();
  if ((!campsOn() && !dispersedOn()) || !state.focused) return;
  const { lat, lng } = state.focused;
  let sites: CampSite[];
  try {
    sites = await getJson("/api/camps", { query: { lat, lng, free_only: freeOnly() } });
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  sites.forEach((site) => {
    const dispersed = isDispersed(site);
    if (dispersed ? !dispersedOn() : !campsOn()) return; // gated by the matching toggle
    const isFree = site.free === true;
    const marker = L.circleMarker(
      [site.center_lat, site.center_lng],
      circleStyle({
        radius: dispersed ? 6 : 5,
        fill: dispersed ? CAMP_OSM : isFree ? CAMP_FREE : CAMP_PAID,
        stroke: HOME_RING,
        weight: 1,
        fillOpacity: 0.9,
      }),
    )
      .addTo(map)
      .bindPopup(campPopup(site));
    addCampMarker(marker);
  });
}

// `site.name` and the fee text come from an external API (buildPopup sets them via textContent);
// `site.url` is server-constructed (recreation.gov / openstreetmap + id), so it's a safe href.
function campPopup(site: CampSite): HTMLElement {
  const isOsm = site.source === "osm";
  const detail = feeLabel(site.free === true, site.fee);
  return buildPopup({
    title: site.name,
    lines: [`${dist(site.distance_km)} · ${detail}`],
    link: { href: site.url, text: isOsm ? "OpenStreetMap ↗" : "Recreation.gov ↗" },
  });
}

// Fetch + shade public-land ownership across the whole search radius (not just the focused
// destination) - land ownership doesn't change per-destination, so show everywhere there's
// ingested data instead of a tight circle around whichever result happens to be focused.
// No-op (just clears) when the toggle is off. Polygons sit behind the observation/campground
// markers and degrade quietly.
export async function loadLand(): Promise<void> {
  clearLand();
  renderLegend();
  if (!landOn() || !state.home) return;
  const { lat, lng, radius_km } = state.home;
  let units: LandUnit[];
  try {
    // The generated OpenAPI schema types `geometry` as an opaque `{[key: string]: unknown}`
    // (components["schemas"]["LandUnit"] in ./api/schema) - it's real GeoJSON at runtime, just
    // not modeled further on the backend. `./api/types`'s `LandUnit` alias overrides it to
    // `GeoJSON.Geometry`, hence this cast.
    units = (await getJson("/api/land", { query: { lat, lng, radius_km } })) as unknown as LandUnit[];
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  const layer = L.geoJSON(undefined, {
    style: (feature) => {
      const agency = (feature?.properties as LandUnit | undefined)?.agency ?? "";
      const color = LAND_COLORS[agency] ?? LAND_DEFAULT;
      return { color, weight: 1, fillColor: color, fillOpacity: 0.18, bubblingMouseEvents: false };
    },
    onEachFeature: (feature, lyr) => lyr.bindPopup(landPopup(feature.properties as LandUnit)),
  });
  // Carry each unit's fields as GeoJSON `properties` so style/popup can read them. Each agency
  // has its own toggle, so a fetched-but-toggled-off agency is filtered out here rather than
  // re-fetched per toggle - one request covers whichever combination is on.
  units.forEach((unit) => {
    const isOn = LAND_TOGGLES[unit.agency];
    if (isOn && !isOn()) return;
    const feature: GeoJSON.Feature = {
      type: "Feature",
      properties: unit,
      geometry: unit.geometry,
    };
    layer.addData(feature);
  });
  layer.addTo(map);
  layer.bringToBack(); // keep observation + campground markers clickable on top
  setLandLayer(layer);
}

export const fireOn = (): boolean => qs<HTMLInputElement>("#show-fire").checked;

// Wildfire overlay (issue #227): active perimeters/points red, recent burn scars burnt-orange
// and dimmed for older years. Fetched around home (like public land, not per-focused-region).
// Informational only - the popup links the official incident page, asserts no closure.
export async function loadFire(): Promise<void> {
  clearFire();
  renderLegend();
  if (!fireOn() || !state.home) return;
  const { lat, lng, radius_km } = state.home;
  let fires: FireNear[];
  try {
    fires = (await getJson("/api/fire", { query: { lat, lng, radius_km } })) as unknown as FireNear[];
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  const scarOpacity = (year: number | null): number => {
    const age = year ? new Date().getFullYear() - year : 3;
    return age <= 1 ? 0.35 : age === 2 ? 0.22 : 0.12;
  };
  const layer = L.geoJSON(undefined, {
    style: (feature) => {
      const fire = feature?.properties as FireNear | undefined;
      if (fire?.status === "active") {
        return {
          color: FIRE_ACTIVE,
          weight: 2,
          fillColor: FIRE_ACTIVE,
          fillOpacity: 0.25,
          bubblingMouseEvents: false,
        };
      }
      return {
        color: FIRE_SCAR,
        weight: 1,
        fillColor: FIRE_SCAR,
        fillOpacity: scarOpacity((fire?.fire_year as number | null) ?? null),
        bubblingMouseEvents: false,
      };
    },
    pointToLayer: (feature, latlng) =>
      L.circleMarker(
        latlng,
        circleStyle({
          radius: 6,
          fill: (feature.properties as FireNear).status === "active" ? FIRE_ACTIVE : FIRE_SCAR,
          fillOpacity: 0.9,
        }),
      ),
    onEachFeature: (feature, lyr) => lyr.bindPopup(firePopup(feature.properties as FireNear)),
  });
  fires.forEach((fire) => {
    if (!fire.geometry) return;
    const feature: GeoJSON.Feature = { type: "Feature", properties: fire, geometry: fire.geometry };
    layer.addData(feature);
  });
  layer.addTo(map);
  layer.bringToBack();
  setFireLayer(layer);
}

// `fire.name` comes from an external service (buildPopup sets it via textContent); the incident
// url is server-constructed (InciWeb / NIFC).
function firePopup(fire: FireNear): HTMLElement {
  const bits: string[] = [];
  if (fire.status === "active") {
    bits.push("Active fire");
    if (fire.percent_contained != null) bits.push(`${Math.round(fire.percent_contained)}% contained`);
  } else {
    bits.push(fire.fire_year ? `${fire.fire_year} burn scar` : "Burn scar");
    if (fire.dominant_severity) bits.push(`${fire.dominant_severity} severity`);
  }
  if (fire.gis_acres != null) bits.push(`${Math.round(fire.gis_acres).toLocaleString()} ac`);
  return buildPopup({
    title: fire.name,
    lines: [bits.join(" · "), "Informational only - check official sources before travel"],
    ...(fire.incident_url ? { link: { href: fire.incident_url, text: "Incident info ↗" } } : {}),
  });
}

// agency/unit come from an external service (buildPopup sets them via textContent); the source
// url is a fixed ArcGIS service link.
function landPopup(unit: LandUnit): HTMLElement {
  return buildPopup({
    title: unit.unit,
    lines: [`${unit.agency} · ownership only, not legal advice`],
    link: { href: unit.url, text: "Source (ArcGIS) ↗" },
  });
}

// A trail's geometry as one or more ordered point sequences ([lat, lng] pairs, Leaflet's order -
// GeoJSON stores [lng, lat]) - a plain LineString is a single part, a MultiLineString (the merged
// way + route relation trailhead_network can return, see trails.py) is drawn as multiple parts in
// sequence rather than joined into one, so the animation doesn't fake a connection between
// segments that aren't actually contiguous on the ground.
function trailParts(geometry: GeoJSON.Geometry): L.LatLngTuple[][] {
  if (geometry.type === "LineString") {
    return [geometry.coordinates.map(([lng, lat]) => [lat, lng] as L.LatLngTuple)];
  }
  if (geometry.type === "MultiLineString") {
    return geometry.coordinates.map((line) => line.map(([lng, lat]) => [lat, lng] as L.LatLngTuple));
  }
  return [];
}

// Only the most recently started animation should still be drawing - if the user clicks a
// different trailhead mid-animation, clearSelectedTrail() already removed the in-progress layer
// from the map, but without this guard the orphaned rAF loop would keep computing frames for a
// layer nobody sees until it finishes on its own.
const trailAnimationGuard = createRunGuard();

// Progressively reveals `parts` on `layer` over a short, point-count-scaled duration (capped so a
// simple trailhead-to-junction segment and a long merged route both feel like "watching it get
// drawn" rather than either an instant snap or a sluggish crawl). Parts fill in order - later
// parts stay empty until earlier ones finish - so a multi-segment trail reads as one continuous
// draw across its pieces.
function animateTrail(layer: L.Polyline, parts: L.LatLngTuple[][]): void {
  const isCurrent = trailAnimationGuard.begin();
  const totalPoints = parts.reduce((sum, part) => sum + part.length, 0);
  if (totalPoints === 0) return;
  const duration = Math.min(1400, Math.max(500, totalPoints * 25));
  const start = performance.now();
  const frame = (now: number): void => {
    if (!isCurrent()) return; // superseded by a newer selection
    const elapsed = now - start;
    const revealed = Math.min(totalPoints, Math.ceil((elapsed / duration) * totalPoints));
    let remaining = revealed;
    const shown: L.LatLngTuple[][] = [];
    for (const part of parts) {
      if (remaining <= 0) break;
      const take = Math.min(part.length, remaining);
      shown.push(part.slice(0, take));
      remaining -= take;
    }
    layer.setLatLngs(shown);
    if (revealed < totalPoints) requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

// Draws the real trail for a trailhead selected in a destination card's Trails tab (views.ts).
// Fetches `/api/trails/network`, which resolves it via live OSM topology when the trailhead sits
// on a real way/route, falling back to the nearest already-cached path/route otherwise - drawn
// solid for the former, dashed for the latter so the UI doesn't overstate confidence in a guess.
// At most one selected trail shows at a time (state.selectedTrailLayer). Zooms to the trailhead
// itself right away - the live Overpass lookup (trailhead_network) can take a beat, and waiting
// for it before moving the camera reads as the whole thing being slow, not just the data. Once
// the real geometry arrives, the view re-fits to the trail's actual extent and animates the line
// drawing in (animateTrail) rather than snapping it in instantly.
export async function selectTrailhead(trail: Trail): Promise<void> {
  clearSelectedTrail();
  map.flyTo([trail.center_lat, trail.center_lng], Math.max(map.getZoom(), 14), { duration: 0.5 });
  let path: TrailPath;
  try {
    // See the LandUnit cast above - `geometry` is real GeoJSON, just untyped on the backend.
    path = (await getJson("/api/trails/network", {
      query: { trail_id: trail.id },
    })) as unknown as TrailPath;
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  // /api/trails/network always resolves a geometry (live OSM topology or the nearest cached
  // trail); the null case only exists for the geometry-less /api/trails list rows.
  if (!path.trail.geometry) return;
  const parts = trailParts(path.trail.geometry);
  if (!parts.length) return;
  const layer = L.polyline([], {
    color: TRAIL,
    weight: 3,
    opacity: 0.9,
    dashArray: path.authoritative ? undefined : "6 6",
    bubblingMouseEvents: false,
  }).addTo(map);
  setSelectedTrail(layer);
  map.flyToBounds(L.latLngBounds(parts.flat()), { padding: [40, 40], maxZoom: 15, duration: 0.5 });
  animateTrail(layer, parts);
}

// Fetch + plot individually-precise observations (issue #161): unlike the coarse cell_deg
// circles plot() draws for every region, `obscured = false` rows have a cached coordinate
// that's been live-verified against iNat as the real find location, not a randomized
// geoprivacy decoy - worth showing as its own small pin. On by default (no layer toggle) and
// scoped to the *focused* destination's own footprint (regionRadiusKm() - the same true-size
// circle selectSize snaps that region's bubble to), not the whole search radius - an earlier
// version fetched radius-wide and put a cluster badge on every destination on the map at once,
// which visually buried the (much smaller, score-scaled) destination bubbles the badges were
// sitting on top of. Scoping to one region at a time makes precise pins read as that
// destination's detail view instead of a second, competing map layer, and keeps the result set
// small enough that unlike other layers, it needs no opt-in. No-op (just clears) when nothing's
// focused. Pins go into the cluster group (map.ts's preciseCluster) instead of straight onto the
// map - a dense area folds into a count badge instead of dumping hundreds of overlapping dots.
export async function loadPreciseObservations(): Promise<void> {
  clearPrecise();
  renderLegend();
  if (!state.focused) return;
  const { lat, lng } = state.focused;
  let observations: PreciseObservation[];
  try {
    observations = await getJson("/api/observations/precise", {
      query: { months: monthsParam(), lat, lng, radius_km: regionRadiusKm() },
    });
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  observations.forEach((obs) => {
    const marker = L.circleMarker(
      [obs.lat, obs.lng],
      circleStyle({ radius: 4, fill: PRECISE, stroke: HOME_RING, weight: 1, fillOpacity: 0.9 }),
    ).bindPopup(precisePopup(obs));
    addPreciseMarker(marker);
  });
}

// name/observed_on come from an external API (buildPopup sets them via textContent); `obs.uri`
// is server-constructed (a fixed iNaturalist observation URL).
function precisePopup(obs: PreciseObservation): HTMLElement {
  return buildPopup({
    title: displayName(obs),
    lines: obs.observed_on ? [obs.observed_on] : [],
    ...(obs.uri ? { link: { href: obs.uri, text: "iNaturalist ↗" } } : {}),
  });
}

// Public land depends only on state.home (whole search radius), not the focused destination, so
// it's loaded whenever home changes (see call sites of updateHome) rather than on every
// focusRegion() call - otherwise every destination click/auto-focus re-fetches identical polygons.
export function focusRegion(lat: number, lng: number): void {
  setFocused(lat, lng);
  loadCamps();
  loadPreciseObservations();
}
