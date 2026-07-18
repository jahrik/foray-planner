import L from "leaflet";

import { getJson } from "./api/client";
import type { Stop, TripPlan } from "./api/types";
import { focusRegion } from "./layers";
import { clearMarkers, map, PLAN_STOP } from "./map";
import { dist, errorDetail, inatUrl, MONTHS, qs, setStatus, state } from "./state";
import { monthsParam } from "./views";

export async function runPlan(): Promise<void> {
  setStatus("Planning route…");
  clearMarkers();

  const maxStops = Math.max(1, Math.min(20,
    Math.round((document.getElementById("plan-stops") as HTMLInputElement).valueAsNumber) || 5,
  ));
  const maxDrive = Math.max(50,
    (document.getElementById("plan-drive") as HTMLInputElement).valueAsNumber || 400,
  );
  const requireFree = (document.getElementById("plan-free-camp") as HTMLInputElement).checked;

  let trip: TripPlan;
  try {
    trip = await getJson("/api/plan", {
      query: {
        months: monthsParam(),
        max_stops: maxStops,
        max_drive_km: maxDrive,
        require_free_camp: requireFree,
      },
    });
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  state.planTrip = trip;

  const panel = qs("#panel");
  if (!trip.stops.length) {
    panel.innerHTML =
      "<p class='hint'>No viable route found. Try relaxing constraints (disable 'Require free camp', increase max leg km, or run Refresh).</p>";
    setStatus("");
    return;
  }

  // Route polyline: home → stop1 → stop2 → …
  const routePoints: L.LatLngExpression[] = [
    [trip.home_lat, trip.home_lng],
    ...trip.stops.map((stop): L.LatLngExpression => [stop.center_lat, stop.center_lng]),
  ];
  state.planRouteLayer = L.polyline(routePoints, {
    color: PLAN_STOP,
    weight: 2.5,
    opacity: 0.7,
    dashArray: "8 5",
    bubblingMouseEvents: false,
  }).addTo(map);

  // Plot stop markers (reuse plot() then re-colour to gold). Build the popup
  // with DOM nodes so common_name values from the external API are never
  // injected as raw HTML.
  trip.stops.forEach((stop) => {
    const popupEl = document.createElement("div");
    const title = document.createElement("b");
    title.textContent = `Stop ${stop.order}`;
    const drive = document.createTextNode(` · ${dist(stop.drive_km_from_prev)} leg`);
    const br = document.createElement("br");
    const names = document.createTextNode(
      stop.species.slice(0, 3).map((hit) => hit.common_name).join(", "),
    );
    popupEl.append(title, drive, br, names);
    const marker = L.circleMarker([stop.center_lat, stop.center_lng], {
      radius: 6 + 14 * stop.score_norm,
      color: PLAN_STOP,
      fillColor: PLAN_STOP,
      fillOpacity: 0.6,
      weight: 1.5,
      bubblingMouseEvents: false,
    })
      .addTo(map)
      .bindPopup(popupEl);
    state.markers.push(marker);
  });

  // Fit the map to the full route.
  map.fitBounds(L.latLngBounds(routePoints), { padding: [40, 40] });

  // Build the panel.
  const monthNames = trip.months.map((month) => MONTHS[month - 1]).join(", ");
  const skippedNote = trip.skipped_unreachable
    ? ` <span class="plan-skipped">${trip.skipped_unreachable} skipped (too far)</span>`
    : "";
  panel.innerHTML = `
    <div class="plan-header">
      <div class="plan-summary">
        <strong>${trip.n_stops} stops</strong> · ${dist(trip.total_drive_km)} total · ${monthNames}${skippedNote}
      </div>
      <div class="plan-export">
        <button id="export-gpx" class="primary">⬇ GPX</button>
        <button id="export-json">⬇ JSON</button>
      </div>
    </div>
  `;
  trip.stops.forEach((stop) => panel.appendChild(buildStopCard(stop)));

  // Wire export buttons - trip is captured in closure.
  document.getElementById("export-gpx")!.onclick = () => exportGpx(trip);
  document.getElementById("export-json")!.onclick = () => exportJson(trip);

  setStatus(`${trip.n_stops} stops · ${dist(trip.total_drive_km)}`);
}

/** Build a per-stop card using DOM methods so user-controlled text is never injected as HTML. */
function buildStopCard(stop: Stop): HTMLElement {
  const card = document.createElement("div");
  card.className = "stop-card";

  // Header row: stop number + drive distance.
  const head = document.createElement("div");
  head.className = "stop-head";
  const numEl = document.createElement("span");
  numEl.className = "stop-num";
  numEl.textContent = `Stop ${stop.order}`;
  const driveEl = document.createElement("span");
  driveEl.className = "stop-drive";
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
  meta.textContent = `score ${stop.score_norm.toFixed(2)} · ${stop.n_species} spp · ${
    stop.recent_count ? `${stop.recent_count} recent` : "no recent obs"
  }`;
  card.appendChild(meta);

  // Species chips (top 5) - built as DOM nodes so common_name/label from
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
    anchor.textContent = `${hit.common_name} · ${(hit.w_pheno * 100).toFixed(0)}%`;
    chips.appendChild(anchor);
  });
  card.appendChild(chips);

  // Camp info.
  const campEl = document.createElement("div");
  campEl.className = stop.camp ? "stop-camp" : "stop-camp muted";
  if (stop.camp) {
    const campName = document.createElement("strong");
    campName.textContent = stop.camp.name;
    const costText = stop.camp_is_free
      ? "free"
      : stop.camp.fee
        ? stop.camp.fee
        : "cost unknown";
    campEl.append("🏕️ ", campName, ` · ${dist(stop.camp.distance_km)} · ${costText}`);
  } else {
    campEl.textContent = "No camp in range";
  }
  card.appendChild(campEl);

  // Click → zoom the map to this stop and load layers around it.
  card.onclick = () => {
    map.setView([stop.center_lat, stop.center_lng], 10);
    focusRegion(stop.center_lat, stop.center_lng);
  };

  return card;
}

/** Export the trip plan as a GPX file with one waypoint per stop (camp if available). */
function exportGpx(trip: TripPlan): void {
  const monthNames = trip.months.map((month) => MONTHS[month - 1]).join("-");
  const wpts = trip.stops
    .map((stop) => {
      const lat = stop.camp ? stop.camp.center_lat : stop.center_lat;
      const lng = stop.camp ? stop.camp.center_lng : stop.center_lng;
      const name = stop.camp ? `Stop ${stop.order}: ${stop.camp.name}` : `Stop ${stop.order}`;
      const desc = `${stop.species
        .slice(0, 3)
        .map((hit) => hit.common_name)
        .join(", ")} · ${dist(stop.drive_km_from_prev)} leg`;
      return `  <wpt lat="${lat.toFixed(6)}" lon="${lng.toFixed(6)}">\n    <name>${escXml(name)}</name>\n    <desc>${escXml(desc)}</desc>\n  </wpt>`;
    })
    .join("\n");

  const gpx = `<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Foray Planner" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata>
    <name>${escXml(`Foray Trip ${monthNames}`)}</name>
    <desc>${escXml(`${trip.n_stops} stops, ${dist(trip.total_drive_km)}`)}</desc>
  </metadata>
${wpts}
</gpx>`;
  downloadFile("foray-trip.gpx", gpx, "application/gpx+xml");
}

/** Export the trip plan as a pretty-printed JSON file. */
function exportJson(trip: TripPlan): void {
  downloadFile("foray-trip.json", JSON.stringify(trip, null, 2), "application/json");
}

/** Minimal XML entity escaping for text that goes into GPX element content. */
function escXml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
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
