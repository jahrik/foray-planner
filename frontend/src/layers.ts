import L from "leaflet";

import { getJson } from "./api/client";
import type { CampSite, LandUnit, Trail } from "./api/types";
import {
  CAMP_FREE,
  CAMP_OSM,
  CAMP_PAID,
  clearCamps,
  clearLand,
  clearTrails,
  HOME_RING,
  LAND_COLORS,
  LAND_DEFAULT,
  map,
  renderLegend,
  TRAIL,
} from "./map";
import { dist, errorDetail, qs, setStatus, state } from "./state";

export const campsOn = (): boolean => qs<HTMLInputElement>("#show-camps").checked;
export const dispersedOn = (): boolean => qs<HTMLInputElement>("#show-dispersed").checked;
export const freeOnly = (): boolean => qs<HTMLInputElement>("#free-camps").checked;
export const landOn = (): boolean => qs<HTMLInputElement>("#show-land").checked;
export const trailsOn = (): boolean => qs<HTMLInputElement>("#show-trails").checked;

// OSM dispersed layer: real tagged sites ("reported") + the road∩public-land proxy ("dispersed").
const isDispersed = (site: CampSite): boolean =>
  site.kind === "dispersed" || site.kind === "reported";

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
    sites = await getJson<CampSite[]>(
      `/api/camps?lat=${lat}&lng=${lng}&free_only=${freeOnly()}`,
    );
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  sites.forEach((site) => {
    const dispersed = isDispersed(site);
    if (dispersed ? !dispersedOn() : !campsOn()) return; // gated by the matching toggle
    const proxy = site.kind === "dispersed"; // inferred point (vs a tagged "reported" site)
    const isFree = site.free === true;
    const marker = L.circleMarker([site.center_lat, site.center_lng], {
      radius: dispersed ? 6 : 5,
      color: proxy ? CAMP_OSM : HOME_RING,
      weight: proxy ? 2 : 1,
      dashArray: proxy ? "3 3" : undefined, // dashed ring signals the low-confidence proxy
      fillColor: dispersed ? CAMP_OSM : isFree ? CAMP_FREE : CAMP_PAID,
      fillOpacity: proxy ? 0.35 : 0.9,
      bubblingMouseEvents: false,
    })
      .addTo(map)
      .bindPopup(campPopup(site));
    state.campMarkers.push(marker);
  });
}

// Build the camp popup from DOM nodes rather than an HTML string: `site.name` and the fee text
// come from an external API, so `textContent` escapes them instead of injecting raw HTML.
// `site.url` is server-constructed (recreation.gov / openstreetmap + id), so it's a safe href.
function campPopup(site: CampSite): HTMLElement {
  const isOsm = site.source === "osm";
  // The proxy is a guess, so its detail line carries the "verify" caveat instead of a cost.
  const detail =
    site.kind === "dispersed"
      ? "likely dispersed-legal - verify with the agency"
      : site.free === true
        ? "free"
        : site.fee
          ? site.fee
          : "cost unknown";
  const root = document.createElement("div");
  const title = document.createElement("b");
  title.textContent = site.name;
  const link = document.createElement("a");
  link.href = site.url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = isOsm ? "OpenStreetMap ↗" : "Recreation.gov ↗";
  root.append(
    title,
    document.createElement("br"),
    document.createTextNode(`${dist(site.distance_km)} · ${detail}`),
    document.createElement("br"),
    link,
  );
  return root;
}

// Fetch + shade public-land ownership across the whole search radius (not just the focused
// destination) - land ownership doesn't change per-destination, so show everywhere there's
// ingested data instead of a tight circle around whichever result happens to be focused.
// No-op (just clears) when the toggle is off. Polygons sit behind the observation/campground
// markers and degrade quietly.
export async function loadLand(): Promise<void> {
  clearLand();
  if (!landOn() || !state.home) return;
  const { lat, lng, radius_km } = state.home;
  let units: LandUnit[];
  try {
    units = await getJson<LandUnit[]>(`/api/land?lat=${lat}&lng=${lng}&radius_km=${radius_km}`);
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
  // Carry each unit's fields as GeoJSON `properties` so style/popup can read them.
  units.forEach((unit) => {
    const feature: GeoJSON.Feature = {
      type: "Feature",
      properties: unit,
      geometry: unit.geometry,
    };
    layer.addData(feature);
  });
  layer.addTo(map);
  layer.bringToBack(); // keep observation + campground markers clickable on top
  state.landLayer = layer;
}

// Popup built from DOM nodes: agency/unit come from an external service, so `textContent`
// escapes them; the source url is a fixed ArcGIS service link.
function landPopup(unit: LandUnit): HTMLElement {
  const root = document.createElement("div");
  const title = document.createElement("b");
  title.textContent = unit.unit;
  const link = document.createElement("a");
  link.href = unit.url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "Source (ArcGIS) ↗";
  root.append(
    title,
    document.createElement("br"),
    document.createTextNode(`${unit.agency} · ownership only, not legal advice`),
    document.createElement("br"),
    link,
  );
  return root;
}

// Fetch + draw the OSM trail network around the focused region. Paths/routes render as red
// polylines; trailheads as small red dots. No-op (just clears) when the toggle is off. Trails sit
// above the land shading but below the observation/campground markers, and degrade quietly.
export async function loadTrails(): Promise<void> {
  clearTrails();
  renderLegend();
  if (!trailsOn() || !state.focused) return;
  const { lat, lng } = state.focused;
  let found: Trail[];
  try {
    found = await getJson<Trail[]>(`/api/trails?lat=${lat}&lng=${lng}`);
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  const layer = L.geoJSON(undefined, {
    style: { color: TRAIL, weight: 2, opacity: 0.85, bubblingMouseEvents: false },
    // Trailheads come through as GeoJSON points; render them as small dots instead of pins.
    pointToLayer: (_feature, latlng) =>
      L.circleMarker(latlng, {
        radius: 5,
        color: TRAIL,
        weight: 1,
        fillColor: TRAIL,
        fillOpacity: 0.9,
        bubblingMouseEvents: false,
      }),
    onEachFeature: (feature, lyr) => lyr.bindPopup(trailPopup(feature.properties as Trail)),
  });
  // Carry each trail's fields as GeoJSON `properties` so the popup can read them.
  found.forEach((trail) => {
    const feature: GeoJSON.Feature = {
      type: "Feature",
      properties: trail,
      geometry: trail.geometry,
    };
    layer.addData(feature);
  });
  layer.addTo(map);
  state.trailLayer = layer;
}

// Popup built from DOM nodes: the trail name comes from an external service, so `textContent`
// escapes it; the source url is a fixed openstreetmap.org element link.
function trailPopup(trail: Trail): HTMLElement {
  const root = document.createElement("div");
  const title = document.createElement("b");
  title.textContent = trail.name;
  const camp =
    trail.camp_distance_km != null ? ` · nearest camp ${dist(trail.camp_distance_km)}` : "";
  const link = document.createElement("a");
  link.href = trail.url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "OpenStreetMap ↗";
  root.append(
    title,
    document.createElement("br"),
    document.createTextNode(`${trail.kind} · ${dist(trail.distance_km)} away${camp}`),
    document.createElement("br"),
    link,
  );
  return root;
}

// Public land depends only on state.home (whole search radius), not the focused destination, so
// it's loaded whenever home changes (see call sites of updateHome) rather than on every
// focusRegion() call - otherwise every destination click/auto-focus re-fetches identical polygons.
export function focusRegion(lat: number, lng: number): void {
  state.focused = { lat, lng };
  loadCamps();
  loadTrails();
}
