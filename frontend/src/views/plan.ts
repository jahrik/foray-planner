import L from "leaflet";

import { getJson } from "../api/client";
import type { Stop, TripPlan } from "../api/types";
import { escapeXml, feeLabel, stopsLabel } from "../format";
import {
  directionsLink,
  GOOGLE_MAPS_WAYPOINT_CAP,
  googleMapsRouteUrl,
  type RoutePoint,
} from "../map/directions";
import { focusRegion } from "../map/layers";
import { dockOffsetPx, focusOnMap } from "../map/sheet";
import { addMarker } from "../map/destinations";
import { clearMarkers, map, setPlanRoute, HOME_DOT_STYLE, PLAN_STOP } from "../map/map";
import { circleStyle } from "../map/markers";
import { buildPopup } from "../map/popup";
import { legPoint, pinKindLabel, routeEnd, stopPoint, withoutRepeats } from "./plan-points";
import { openDetails } from "./details";
import { addToShortlist, pinParams, shortlistIds } from "./shortlist";
import { dist, displayName, errorDetail, inatUrl, monthsParam, MONTHS, qs, setStatus, state } from "../state";

export async function runPlan({ reuseCache = false }: { reuseCache?: boolean } = {}): Promise<void> {
  setStatus("Planning route…");
  clearMarkers();

  // Repaint from the last plan (state.planTrip) on a units toggle rather than re-planning -
  // keeps the km/mi flip off the network (issue #301 F2).
  if (reuseCache && state.planTrip) {
    renderPlan(state.planTrip);
    return;
  }

  // Regions the user added with "+ Plan" on a result card are the whole trip (issue #311 -
  // nothing auto-filled around them), each optionally pinned to a specific camp/trail. Empty
  // list = the server auto-picks up to Max stops, so that field only applies then.
  const waypoints = shortlistIds();
  const stopsField = document.getElementById("plan-stops") as HTMLInputElement;
  stopsField.disabled = waypoints.length > 0;
  stopsField.title = waypoints.length > 0 ? "Your picked spots are the stops - clear them to auto-pick" : "";
  const stopsInput = Math.round(stopsField.valueAsNumber);
  const maxStops = Math.max(1, Math.min(20, Number.isNaN(stopsInput) ? 5 : stopsInput));
  const driveInput = (document.getElementById("plan-drive") as HTMLInputElement).valueAsNumber;
  const maxDrive = Math.max(50, Number.isNaN(driveInput) ? 400 : driveInput);
  const requireFree = (document.getElementById("plan-free-camp") as HTMLInputElement).checked;
  const start = (document.getElementById("plan-start") as HTMLInputElement).value.trim();
  const destination = (document.getElementById("plan-destination") as HTMLInputElement).value.trim();
  const pins = pinParams();

  let trip: TripPlan;
  try {
    trip = await getJson("/api/plan", {
      query: {
        months: monthsParam(),
        start: start || null,
        destination: destination || null,
        max_stops: maxStops,
        max_drive_km: maxDrive,
        require_free_camp: requireFree,
        waypoints: waypoints.length ? waypoints.join(",") : null,
        pin: pins.length ? pins : null,
      },
    });
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  state.planTrip = trip;
  renderPlan(trip);
}

function renderPlan(trip: TripPlan): void {
  const panel = qs("#panel");
  if (!trip.stops.length) {
    const reason =
      trip.auto_destination && trip.destination_name === null
        ? "No destination found to auto-pick within range. Try setting a destination manually or run Refresh."
        : "No viable route found. Try relaxing constraints (disable 'Require free camp', increase max leg km, or run Refresh).";
    panel.innerHTML = `<p class='hint'>${reason}</p>`;
    setStatus("");
    return;
  }

  // Route polyline: start → stop1 → stop2 → … → destination, through each stop's pinned
  // feature when it has one (the same points the server measured the legs between). A trip that
  // ends at its last picked stop doesn't repeat it as a separate destination point.
  const end = routeEnd(trip);
  const routePoints: L.LatLngExpression[] = [
    [trip.start_lat, trip.start_lng],
    ...trip.stops.map((stop): L.LatLngExpression => {
      const point = legPoint(stop);
      return [point.lat, point.lng];
    }),
    ...(end.lastStopIsDestination ? [] : [[end.point.lat, end.point.lng] as L.LatLngExpression]),
  ];
  setPlanRoute(
    L.polyline(routePoints, {
      color: PLAN_STOP,
      weight: 2.5,
      opacity: 0.7,
      dashArray: "8 5",
      bubblingMouseEvents: false,
    }).addTo(map),
  );

  // Start marker (matches the persistent "you are here" home-dot styling).
  const startMarker = L.circleMarker([trip.start_lat, trip.start_lng], HOME_DOT_STYLE)
    .addTo(map)
    .bindPopup("Start");
  addMarker(startMarker);

  // Destination marker - a larger hollow ring in the plan-stop gold so it reads as the "goal",
  // distinct from the filled stop circles along the way.
  // When the trip ends at its last stop, the ring sits on that stop's end of the route line.
  const destLabel = end.lastStopIsDestination
    ? `Destination · Stop ${trip.stops.length}`
    : trip.auto_destination
      ? `Auto-picked destination${trip.destination_name ? ` (region ${trip.destination_name})` : ""}`
      : "Destination";
  const destPoint = end.lastStopIsDestination ? legPoint(trip.stops[trip.stops.length - 1]!) : end.point;
  const destMarker = L.circleMarker(
    [destPoint.lat, destPoint.lng],
    circleStyle({ radius: 10, fill: PLAN_STOP, weight: 3, fillOpacity: 0.15 }),
  )
    .addTo(map)
    .bindPopup(destLabel);
  addMarker(destMarker);

  // Plot stop markers. buildPopup sets name/common_name values from the external API via
  // textContent so they're never injected as raw HTML.
  trip.stops.forEach((stop) => {
    const names = stop.species
      .slice(0, 3)
      .map((hit) => displayName(hit))
      .join(", ");
    const popupEl = buildPopup({
      title: `Stop ${stop.order}`,
      titleSuffix: ` · ${dist(stop.drive_km_from_prev)} leg`,
      lines: [names],
    });
    const marker = L.circleMarker(
      [stop.center_lat, stop.center_lng],
      circleStyle({ radius: 6 + 14 * stop.score_norm, fill: PLAN_STOP, fillOpacity: 0.6, weight: 1.5 }),
    )
      .addTo(map)
      .bindPopup(popupEl);
    addMarker(marker);
    // The pinned trailhead / campground itself (issue #311) - a small solid dot on the route.
    if (stop.pin) {
      const pinMarker = L.circleMarker(
        [stop.pin.lat, stop.pin.lng],
        circleStyle({ radius: 5, fill: PLAN_STOP, fillOpacity: 1, weight: 2 }),
      )
        .addTo(map)
        .bindPopup(
          buildPopup({
            title: stop.pin.name,
            titleSuffix: ` · Stop ${stop.order}`,
            lines: [pinKindLabel(stop.pin)],
            directions: directionsLink(stop.pin.lat, stop.pin.lng, stop.pin.name),
          }),
        );
      addMarker(pinMarker);
    }
  });

  // Fit the map to the full route, keeping it clear of the desktop results dock (issue #297)
  // on the left - a symmetric padding would push western legs behind the open panel.
  map.fitBounds(L.latLngBounds(routePoints), {
    paddingTopLeft: [40 + dockOffsetPx(), 40],
    paddingBottomRight: [40, 40],
  });

  // Build the panel.
  const monthNames = trip.months.map((month) => MONTHS[month - 1]).join(", ");
  const skippedNote = trip.skipped_unreachable
    ? ` <span class="plan-skipped">${trip.skipped_unreachable} skipped (too far)</span>`
    : "";
  const autoNote = trip.auto_destination ? ` <span class="plan-auto">auto-picked destination</span>` : "";
  // Google Maps' dir/?api=1 URL silently drops waypoints past GOOGLE_MAPS_WAYPOINT_CAP, so a
  // longer trip disables the button rather than sending a route that quietly loses stops - the
  // GPX export (no such cap) stays the way to get the full route into any other maps app.
  const tooManyStops = trip.stops.length - (end.lastStopIsDestination ? 1 : 0) > GOOGLE_MAPS_WAYPOINT_CAP;
  const gmapsDisabled = tooManyStops
    ? `disabled title="Too many stops for a Google Maps link (max ${GOOGLE_MAPS_WAYPOINT_CAP}) - use the GPX export instead"`
    : "";
  panel.innerHTML = `
    <div class="plan-header">
      <div class="plan-summary">
        <strong class="num">${stopsLabel(trip.n_stops)}</strong> · <span class="num">${dist(trip.total_drive_km)} total</span> · ${monthNames}${skippedNote}${autoNote}
      </div>
      <div class="plan-export">
        <button id="export-gpx" class="primary">⬇ GPX</button>
        <button id="export-json">⬇ JSON</button>
        <button id="export-gmaps" ${gmapsDisabled}>Open in Google Maps</button>
      </div>
    </div>
  `;
  trip.stops.forEach((stop) => panel.appendChild(buildStopCard(stop, trip)));

  // Wire export buttons - trip is captured in closure.
  qs<HTMLButtonElement>("#export-gpx").onclick = () => exportGpx(trip);
  qs<HTMLButtonElement>("#export-json").onclick = () => exportJson(trip);
  qs<HTMLButtonElement>("#export-gmaps").onclick = () => openGoogleMapsRoute(trip);

  setStatus(`${stopsLabel(trip.n_stops)} · ${dist(trip.total_drive_km)}`);
}

/** Build a per-stop card using DOM methods so user-controlled text is never injected as HTML. */
function buildStopCard(stop: Stop, trip: TripPlan): HTMLElement {
  const card = document.createElement("div");
  card.className = "stop-card";

  // Header row: stop number + drive distance.
  const head = document.createElement("div");
  head.className = "stop-head";
  const numEl = document.createElement("span");
  numEl.className = "stop-num num";
  numEl.textContent = `Stop ${stop.order}`;
  const driveEl = document.createElement("span");
  driveEl.className = "stop-drive num";
  driveEl.textContent = `${dist(stop.drive_km_from_prev)} leg · ${dist(stop.cumulative_drive_km)} total`;
  head.append(numEl, driveEl);
  card.appendChild(head);

  // Score bar + meta.
  const barWrap = document.createElement("div");
  barWrap.className = "bar";
  const barFill = document.createElement("span");
  barFill.style.width = `${(stop.score_norm * 100).toFixed(0)}%`;
  barWrap.appendChild(barFill);
  card.appendChild(barWrap);

  const meta = document.createElement("div");
  meta.className = "meta";
  const scoreNum = document.createElement("span");
  scoreNum.className = "num";
  scoreNum.textContent = stop.score_norm.toFixed(2);
  const speciesNum = document.createElement("span");
  speciesNum.className = "num";
  speciesNum.textContent = String(stop.n_species);
  meta.append("score ", scoreNum, " · ", speciesNum, " spp · ");
  if (stop.recent_count) {
    const recentNum = document.createElement("span");
    recentNum.className = "num";
    recentNum.textContent = String(stop.recent_count);
    meta.append(recentNum, " recent");
  } else {
    meta.append("no recent obs");
  }
  card.appendChild(meta);

  // Species chips (top 5) - built as DOM nodes so name/common_name/label from
  // external APIs are set via textContent and never injected as raw HTML.
  const chips = document.createElement("div");
  chips.className = "chips";
  stop.species.slice(0, 5).forEach((hit) => {
    const anchor = document.createElement("a");
    anchor.className = "chip";
    anchor.href = inatUrl(hit.taxon_id);
    anchor.target = "_blank";
    anchor.rel = "noopener";
    anchor.onclick = (ev) => ev.stopPropagation();
    anchor.textContent = `${displayName(hit)} · ${(hit.w_pheno * 100).toFixed(0)}%`;
    chips.appendChild(anchor);
  });
  card.appendChild(chips);

  // The pinned feature, when the user picked one in the Details view (issue #311).
  if (stop.pin) {
    const pinEl = document.createElement("div");
    pinEl.className = "stop-pin";
    const pinName = document.createElement("strong");
    pinName.textContent = stop.pin.name;
    pinEl.append("📍 Stop at ", pinName, ` · ${pinKindLabel(stop.pin)}`);
    card.appendChild(pinEl);
  }

  // Camp info.
  const campEl = document.createElement("div");
  campEl.className = stop.camp ? "stop-camp" : "stop-camp muted";
  if (stop.camp) {
    const campName = document.createElement("strong");
    campName.textContent = stop.camp.name;
    const costText = feeLabel(stop.camp_is_free, stop.camp.fee);
    campEl.append("🏕️ ", campName, ` · ${dist(stop.camp.distance_km)} · ${costText}`);
  } else {
    campEl.textContent = "No camp in range";
  }
  card.appendChild(campEl);

  // Trail info.
  const trailEl = document.createElement("div");
  trailEl.className = stop.trail ? "stop-trail" : "stop-trail muted";
  if (stop.trail) {
    const trailName = document.createElement("strong");
    trailName.textContent = stop.trail.name;
    trailEl.append("🥾 ", trailName, ` · ${dist(stop.trail.distance_km)}`);
  } else {
    trailEl.textContent = "No trail in range";
  }
  card.appendChild(trailEl);

  // Active-fire warning on the corridor stop (issue #227) - burn scars aren't a hazard, so
  // planner.py only threads active fires onto stop.fire_nearby.
  const nearestFire = stop.fire_nearby[0];
  if (nearestFire) {
    const fireEl = document.createElement("div");
    fireEl.className = "fire-warn";
    fireEl.textContent =
      `⚠ Corridor passes ~${dist(nearestFire.distance_km)} from ${nearestFire.name}` +
      (stop.fire_nearby.length > 1 ? ` (+${stop.fire_nearby.length - 1} more active)` : "") +
      " - verify road/forest status officially";
    card.appendChild(fireEl);
  }

  // "Choose stop point" (issue #311): opens this stop's Details view on the Trails tab, where a
  // trail / campground / public-land row can be pinned as the stop's exact point. The current
  // stops are frozen into the shortlist first - otherwise pinning one stop of an auto-picked
  // trip would make the shortlist (and so the whole trip) just that one stop. Back re-plans so
  // the new pin takes effect.
  const actions = document.createElement("div");
  actions.className = "card-actions";
  const choose = document.createElement("button");
  choose.type = "button";
  choose.className = "card-action";
  choose.textContent = stop.pin ? "📍 Change stop point" : "📍 Choose stop point";
  choose.onclick = (event) => {
    event.stopPropagation();
    addToShortlist(trip.stops.map((tripStop) => tripStop.region_id));
    focusOnMap(stop.center_lat, stop.center_lng, 10);
    openDetails(
      { region_id: stop.region_id, center_lat: stop.center_lat, center_lng: stop.center_lng },
      `Stop ${stop.order}`,
      () => void runPlan(),
      { tab: "trails", backLabel: "Back to trip" },
    );
  };
  actions.appendChild(choose);
  card.appendChild(actions);

  // Click → zoom the map to this stop and load layers around it. focusOnMap keeps the stop
  // clear of the desktop dock / mobile sheet (issue #297).
  card.onclick = () => {
    const point = legPoint(stop);
    focusOnMap(point.lat, point.lng, 10);
    focusRegion(stop.center_lat, stop.center_lng);
  };

  return card;
}

function stopName(stop: Stop): string {
  const place = stop.pin?.name ?? stop.camp?.name;
  return place ? `Stop ${stop.order}: ${place}` : `Stop ${stop.order}`;
}

/** Export the trip plan as a GPX file: start, one waypoint per stop (camp if available), destination. */
function exportGpx(trip: TripPlan): void {
  const monthNames = trip.months.map((month) => MONTHS[month - 1]).join("-");
  const stopWpts = trip.stops.map((stop) => {
    const { lat, lng } = stopPoint(stop);
    const name = stopName(stop);
    const stopFire = stop.fire_nearby[0];
    const fireNote = stopFire
      ? ` · ⚠ active fire ~${dist(stopFire.distance_km)} away (verify officially)`
      : "";
    const desc = `${stop.species
      .slice(0, 3)
      .map((hit) => displayName(hit))
      .join(", ")} · ${dist(stop.drive_km_from_prev)} leg${fireNote}`;
    return wptXml(lat, lng, name, desc);
  });
  const destName = trip.auto_destination ? "Destination (auto-picked)" : "Destination";
  const end = routeEnd(trip);
  const wpts = [
    wptXml(trip.start_lat, trip.start_lng, "Start", ""),
    ...stopWpts,
    ...(end.lastStopIsDestination ? [] : [wptXml(end.point.lat, end.point.lng, destName, "")]),
  ].join("\n");

  const gpx = `<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Foray Planner" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata>
    <name>${escapeXml(`Foray Trip ${monthNames}`)}</name>
    <desc>${escapeXml(`${stopsLabel(trip.n_stops)}, ${dist(trip.total_drive_km)}`)}</desc>
  </metadata>
${wpts}
</gpx>`;
  downloadFile("foray-trip.gpx", gpx, "application/gpx+xml");
}

function wptXml(lat: number, lng: number, name: string, desc: string): string {
  const descTag = desc ? `\n    <desc>${escapeXml(desc)}</desc>` : "";
  return `  <wpt lat="${lat.toFixed(6)}" lon="${lng.toFixed(6)}">\n    <name>${escapeXml(name)}</name>${descTag}\n  </wpt>`;
}

/** Export the trip plan as a pretty-printed JSON file. */
function exportJson(trip: TripPlan): void {
  downloadFile("foray-trip.json", JSON.stringify(trip, null, 2), "application/json");
}

/** Open the whole trip as a Google Maps multi-stop driving route (issue #310 Part 2) - the only
 * maps-app URL that accepts more than one stop, so this is additive to the GPX/JSON exports
 * rather than a replacement for them. */
function openGoogleMapsRoute(trip: TripPlan): void {
  const origin: RoutePoint = { lat: trip.start_lat, lng: trip.start_lng };
  const end = routeEnd(trip);
  const stops = end.lastStopIsDestination ? trip.stops.slice(0, -1) : trip.stops;
  // Collapse back-to-back duplicates (two stops sharing a nearest camp), including a last
  // waypoint that equals the destination.
  const points = withoutRepeats([...stops.map(stopPoint), end.point]);
  window.open(googleMapsRouteUrl(origin, end.point, points.slice(0, -1)), "_blank", "noopener");
}

/** Trigger a client-side file download without a round-trip to the server. */
function downloadFile(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  // Defer revocation so the browser has time to initiate the download before
  // the object URL is released (synchronous revoke can truncate on Safari).
  setTimeout(() => URL.revokeObjectURL(url), 100);
}
