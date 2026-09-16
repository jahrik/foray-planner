import L from "leaflet";

import { getJson } from "../api/client";
import type { CampSite, PreciseObservation, Trail, TrailPath } from "../api/types";
import { createRunGuard } from "../ui/card-select";
import { feeLabel } from "../format";
import { LAND_AGENCIES } from "./basemap-land";
import { FORAGE_RAMP, forageTier } from "./forage";
import { isRoughSurface } from "./trail-attrs";
import { circleStyle } from "./markers";
import { buildPopup } from "./popup";
import {
  addCampMarker,
  addPreciseMarker,
  CAMP_FREE,
  CAMP_OSM,
  CAMP_PAID,
  clearCamps,
  clearPrecise,
  clearSelectedTrail,
  HOME_RING,
  map,
  markerPalette,
  regionRadiusKm,
  renderLegend,
  setFireVisibility,
  setFocused,
  setLandVisibility,
  setSelectedTrail,
  TRAIL,
  TRAIL_WALKIN,
} from "./map";
import { dist, displayName, errorDetail, monthsParam, qs, setStatus, state } from "../state";

export const campsOn = (): boolean => qs<HTMLInputElement>("#show-camps").checked;
export const dispersedOn = (): boolean => qs<HTMLInputElement>("#show-dispersed").checked;
export const freeOnly = (): boolean => qs<HTMLInputElement>("#free-camps").checked;
export const blmOn = (): boolean => qs<HTMLInputElement>("#show-land-blm").checked;
export const usfsOn = (): boolean => qs<HTMLInputElement>("#show-land-usfs").checked;
export const tribalOn = (): boolean => qs<HTMLInputElement>("#show-land-tribal").checked;
const LAND_TOGGLES: Record<string, () => boolean> = { BLM: blmOn, USFS: usfsOn, Tribal: tribalOn };

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
  const detail = feeLabel(site.free === true, site.fee, site.fee_low, site.fee_high);
  return buildPopup({
    title: site.name,
    lines: [`${dist(site.distance_km)} · ${detail}`],
    link: { href: site.url, text: isOsm ? "OpenStreetMap ↗" : "Recreation.gov ↗" },
  });
}

// Show/hide the public-land vector layer for whichever agencies are toggled on (issue #336 PR
// 2) - nationwide, unlike the old per-home-radius `/api/land` fetch, so this is a synchronous
// style update, not a network round-trip. Kept as a plain function (not async) since main.ts/
// refresh.ts call it unawaited alongside the camps/precise loaders it used to run next to.
export function loadLand(): void {
  renderLegend();
  setLandVisibility(LAND_AGENCIES.filter((agency) => LAND_TOGGLES[agency]?.()));
}

export const fireOn = (): boolean => qs<HTMLInputElement>("#show-fire").checked;

// Show/hide the wildfire/burn-scar vector layer (issue #227, moved to vector tiles in #336 PR
// 2) - same reasoning as loadLand: a style toggle, not a fetch.
export function loadFire(): void {
  renderLegend();
  setFireVisibility(fireOn());
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

// Draws the real trail for a given trail id, from a destination card's Trails tab
// (selectTrailhead below). Fetches `/api/trails/network`, which resolves
// via live OSM topology when the trail sits on a real way/route, falling back to the nearest
// already-cached path/route otherwise - drawn solid for the former, dashed for the latter so the
// UI doesn't overstate confidence in a guess. At most one selected trail shows at a time
// (state.selectedTrailLayer). Zooms to `flyLat`/`flyLng` right away - the live Overpass lookup
// (trailhead_network) can take a beat, and waiting for it before moving the camera reads as the
// whole thing being slow, not just the data. Once the real geometry arrives, the view re-fits to
// the trail's actual extent and animates the line drawing in (animateTrail) rather than snapping
// it in instantly. `requestedName` labels the non-authoritative "nearest mapped trail" fallback
// message.
async function drawSelectedTrail(
  id: string,
  flyLat: number,
  flyLng: number,
  requestedName: string,
): Promise<void> {
  clearSelectedTrail();
  map.flyTo([flyLat, flyLng], Math.max(map.getZoom(), 14), { duration: 0.5 });
  let path: TrailPath;
  try {
    path = (await getJson("/api/trails/network", { query: { trail_id: id } })) as unknown as TrailPath;
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  // /api/trails/network always resolves a geometry (live OSM topology or the nearest cached
  // trail); the null case only exists for the geometry-less /api/trails list rows.
  if (!path.trail.geometry) return;
  const parts = trailParts(path.trail.geometry);
  if (!parts.length) return;
  const t = path.trail;
  // Style by what the trail *is* (issue A4b): a walk-in gated road draws in a distinct hue from a
  // drivable one; a rough/ungraded surface draws dashed. The "this is a proximity guess" dash
  // (issue #306 C3) still wins when the result isn't authoritative.
  const rough = isRoughSurface(t.attrs);
  // Foraging-density ramp (issue A4c): more research-grade fungi records hugging the line -> a
  // hotter colour. walk_in is a categorical override (keeps its teal); an un-backfilled trail
  // (tier 0) keeps the default red rather than reading as "barren".
  const tier = forageTier(t.forage_obs);
  const trailColor = t.walk_in ? TRAIL_WALKIN : tier > 0 ? FORAGE_RAMP[tier - 1] : TRAIL;
  const layer = L.polyline([], {
    color: trailColor,
    // A named hiking route reads as a heavier line than a lone path (issue #306); a walk-in road
    // sits between the two.
    weight: t.kind === "route" ? 5 : t.walk_in ? 4 : 3,
    opacity: 0.9,
    dashArray: !path.authoritative ? "6 6" : rough ? "10 6" : undefined,
    bubblingMouseEvents: false,
  }).addTo(map);
  // A non-authoritative result is the nearest *different* mapped trail, not the selection's own
  // path - say so plainly rather than letting a stand-in trail read as the one that was picked
  // (issue #306 C3).
  if (!path.authoritative) {
    const label = `Nearest mapped trail: ${path.trail.name} (exact path for “${requestedName}” unavailable)`;
    // OSM names are untrusted - bind the tooltip as a text node, not an HTML string.
    const tip = document.createElement("span");
    tip.textContent = label;
    layer.bindTooltip(tip, { sticky: true });
    setStatus(label);
  }
  setSelectedTrail(layer, t.walk_in === true, t.walk_in ? 0 : tier);
  renderLegend(); // surface / hide the walk-in + foraging-density legend entries
  map.flyToBounds(L.latLngBounds(parts.flat()), { padding: [40, 40], maxZoom: 15, duration: 0.5 });
  animateTrail(layer, parts);
}

/** A destination card's Trails tab row was selected (views.ts / destination-tabs.ts). */
export function selectTrailhead(trail: Trail): Promise<void> {
  return drawSelectedTrail(trail.id, trail.center_lat, trail.center_lng, trail.name);
}

// Fetch + plot individually-precise observations (issue #161): unlike the coarse per-region
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
  const spore = markerPalette().spore;
  observations.forEach((obs) => {
    const marker = L.circleMarker(
      [obs.lat, obs.lng],
      circleStyle({ radius: 4, fill: spore, stroke: HOME_RING, weight: 1, fillOpacity: 0.9 }),
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
