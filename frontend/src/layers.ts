import L from "leaflet";

import { getJson } from "./api/client";
import type { CampSite, LandUnit, PreciseObservation, Trail, TrailPath, TreeCell } from "./api/types";
import {
  addPreciseMarker,
  CAMP_FREE,
  CAMP_OSM,
  CAMP_PAID,
  clearCamps,
  clearLand,
  clearPrecise,
  clearSelectedTrail,
  clearTrees,
  HOME_RING,
  LAND_COLORS,
  LAND_DEFAULT,
  map,
  plotTree,
  PRECISE,
  regionRadiusKm,
  renderLegend,
  TRAIL,
} from "./map";
import { dist, displayName, errorDetail, monthsParam, qs, setStatus, state } from "./state";

export const campsOn = (): boolean => qs<HTMLInputElement>("#show-camps").checked;
export const dispersedOn = (): boolean => qs<HTMLInputElement>("#show-dispersed").checked;
export const freeOnly = (): boolean => qs<HTMLInputElement>("#free-camps").checked;
export const blmOn = (): boolean => qs<HTMLInputElement>("#show-land-blm").checked;
export const usfsOn = (): boolean => qs<HTMLInputElement>("#show-land-usfs").checked;
export const tribalOn = (): boolean => qs<HTMLInputElement>("#show-land-tribal").checked;
const LAND_TOGGLES: Record<string, () => boolean> = { BLM: blmOn, USFS: usfsOn, Tribal: tribalOn };
const landOn = (): boolean => blmOn() || usfsOn() || tribalOn();
export const treesOn = (): boolean => qs<HTMLInputElement>("#show-trees").checked;

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
    sites = await getJson("/api/camps", { query: { lat, lng, free_only: freeOnly() } });
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

// Fetch + plot host-tree density across the whole search radius (not just the focused
// destination), same "doesn't depend on which card is focused" scoping as loadLand - tree
// density is a property of the ground, not of a particular ranked result. Scoped server-side
// to whichever host tree genera are relevant to this visitor's selected ECM fungi genera (or
// every tracked genus with no selection - see api.py's relevant_tree_genera). No-op (just
// clears) when the toggle is off.
export async function loadTrees(): Promise<void> {
  clearTrees();
  renderLegend();
  if (!treesOn() || !state.home) return;
  const { lat, lng, radius_km } = state.home;
  let cells: TreeCell[];
  try {
    cells = await getJson("/api/trees", { query: { lat, lng, radius_km } });
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  const maxCnt = cells.reduce((max, cell) => Math.max(max, cell.cnt), 1);
  state.treeGenera = new Set(cells.map((cell) => cell.genus));
  renderLegend(); // re-render now that treeGenera reflects this fetch, not the previous one
  cells.forEach((cell) => {
    plotTree(cell.center_lat, cell.center_lng, cell.genus, cell.cnt, maxCnt).bindPopup(treePopup(cell));
  });
}

// The bubble's own color already carries genus identity for the common genera (map.ts's
// TREE_COLORS); the popup spells it out precisely (including for the grey "other" bucket) and
// adds the count. `cell.genus` is a scientific name from GloBI/iNat data, not user input, but
// textContent is used anyway rather than trusting that.
function treePopup(cell: TreeCell): HTMLElement {
  const root = document.createElement("div");
  const title = document.createElement("b");
  title.textContent = cell.genus;
  root.append(title, document.createElement("br"), document.createTextNode(`${dist(cell.distance_km)} · ${cell.cnt} observations`));
  return root;
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
// from the map, but without this token the orphaned rAF loop would keep computing frames for a
// layer nobody sees until it finishes on its own.
let trailAnimationToken = 0;

// Progressively reveals `parts` on `layer` over a short, point-count-scaled duration (capped so a
// simple trailhead-to-junction segment and a long merged route both feel like "watching it get
// drawn" rather than either an instant snap or a sluggish crawl). Parts fill in order - later
// parts stay empty until earlier ones finish - so a multi-segment trail reads as one continuous
// draw across its pieces.
function animateTrail(layer: L.Polyline, parts: L.LatLngTuple[][]): void {
  const token = ++trailAnimationToken;
  const totalPoints = parts.reduce((sum, part) => sum + part.length, 0);
  if (totalPoints === 0) return;
  const duration = Math.min(1400, Math.max(500, totalPoints * 25));
  const start = performance.now();
  const frame = (now: number): void => {
    if (token !== trailAnimationToken) return; // superseded by a newer selection
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
  const parts = trailParts(path.trail.geometry);
  if (!parts.length) return;
  const layer = L.polyline([], {
    color: TRAIL,
    weight: 3,
    opacity: 0.9,
    dashArray: path.authoritative ? undefined : "6 6",
    bubblingMouseEvents: false,
  }).addTo(map);
  state.selectedTrailLayer = layer;
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
    const marker = L.circleMarker([obs.lat, obs.lng], {
      radius: 4,
      color: HOME_RING,
      weight: 1,
      fillColor: PRECISE,
      fillOpacity: 0.9,
      bubblingMouseEvents: false,
    }).bindPopup(precisePopup(obs));
    addPreciseMarker(marker);
  });
}

// Popup built from DOM nodes: name/observed_on come from an external API, so `textContent`
// escapes them; `obs.uri` is server-constructed (a fixed iNaturalist observation URL).
function precisePopup(obs: PreciseObservation): HTMLElement {
  const root = document.createElement("div");
  const title = document.createElement("b");
  title.textContent = displayName(obs);
  root.append(title);
  if (obs.observed_on) {
    root.append(document.createElement("br"), document.createTextNode(obs.observed_on));
  }
  if (obs.uri) {
    root.append(document.createElement("br"));
    const link = document.createElement("a");
    link.href = obs.uri;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "iNaturalist ↗";
    root.append(link);
  }
  return root;
}

// Public land depends only on state.home (whole search radius), not the focused destination, so
// it's loaded whenever home changes (see call sites of updateHome) rather than on every
// focusRegion() call - otherwise every destination click/auto-focus re-fetches identical polygons.
export function focusRegion(lat: number, lng: number): void {
  state.focused = { lat, lng };
  loadCamps();
  loadPreciseObservations();
}
