import L from "leaflet";

import { getJson } from "./api/client";
import type {
  Calendar,
  CampSite,
  RecentObservation,
  RecentObservationsPage,
  RegionScore,
  Trail,
} from "./api/types";
import { selectTrailhead } from "./layers";
import {
  clearCardCampMarkers,
  clearTrailheadMarkers,
  HEAT_RGB,
  plotCardCamp,
  plotTrailhead,
  regionRadiusKm,
  setCardCampActive,
  setTrailheadActive,
} from "./map";
import { escapeHtml, feeLabel } from "./format";
import { dist, displayName, errorDetail, monthsParam, MONTHS, setStatus } from "./state";

// The four detail-tab bodies behind each destination card (Calendar / Photos / Trails /
// Campgrounds). Each function fetches once per card (the caller's createLazyLoader owns the
// fetch-once guard) and renders straight into that card's own tab body rather than a slot
// shared across all cards, and each returns whether it succeeded so a failed load can be
// retried on the tab's next open. Selected from views.ts runDestinations.

// Fetches once per card and renders straight into that card's own calendar-tab body, rather
// than a slot shared across all cards. Returns whether it succeeded so the caller can tell a
// real load from a failed one and allow a retry.
export async function loadCalendarInto(regionId: string, container: HTMLElement): Promise<boolean> {
  container.innerHTML = "<p class='hint'>Loading…</p>";
  let calendar: Calendar;
  try {
    calendar = await getJson("/api/calendar", { query: { region_id: regionId } });
  } catch (error) {
    container.innerHTML = `<p class="hint">${escapeHtml(errorDetail(error))}</p>`;
    return false;
  }
  const peak = Math.max(1, ...Object.values(calendar).map((bucket) => bucket.total));
  let rows = "";
  for (let month = 1; month <= 12; month++) {
    const bucket = calendar[month];
    if (!bucket) continue;
    const fraction = bucket.total / peak;
    const background = `rgba(${HEAT_RGB},${fraction.toFixed(2)})`;
    const speciesText = Object.entries(bucket.species)
      .map(([name, count]) => `${escapeHtml(name)}: ${count}`)
      .join(", ");
    rows += `<tr><td>${MONTHS[month - 1]}</td>
      <td class="heat" style="background:${background}">${bucket.total || ""}</td>
      <td class="meta">${speciesText}</td></tr>`;
  }
  container.innerHTML = `<table class="cal"><tr><th>Month</th><th>Obs</th><th>Species</th></tr>${rows}</table>`;
  return true;
}

function renderObsPhoto(obs: RecentObservation): string {
  const name = displayName(obs);
  const uri = obs.uri && obs.uri.startsWith("https://") ? escapeHtml(obs.uri) : null;
  const link = uri
    ? `<a href="${uri}" target="_blank" rel="noopener">${escapeHtml(name)}</a>`
    : escapeHtml(name);
  const when = obs.observed_on ? escapeHtml(obs.observed_on) : "";
  const photo = obs.photos[0] && obs.photos[0].url.startsWith("https://") ? obs.photos[0] : null;
  const img = photo
    ? `<img class="obs-thumb" src="${escapeHtml(photo.url)}" alt="${escapeHtml(name)}" loading="lazy" />`
    : "";
  const thumb = photo
    ? `${uri ? `<a href="${uri}" target="_blank" rel="noopener">${img}</a>` : img}
       <div class="meta">${escapeHtml(photo.attribution)}</div>`
    : "";
  return `<div class="obs-photo">${thumb}<div class="meta">${link} · ${when}</div></div>`;
}

// Same fetch-once-per-card pattern as loadCalendarInto. Observations without an eligible
// (redisplayable) photo still get listed as a plain link back to iNat, per the license allow-list
// the backend already applied.
//
// The backend caps each page at 12 (issue #174) - a "Load more" button (same stopPropagation
// pattern as the species tab's show-more button, since it's a plain button, not a link
// stopLinkPropagation already covers) fetches the next `offset` page and appends rather than
// re-fetching from scratch, disappearing once the backend reports no further page.
export async function loadPhotosInto(regionId: string, container: HTMLElement): Promise<boolean> {
  container.innerHTML = "<p class='hint'>Loading…</p>";
  // Captured once and reused for every "Load more" click in this paging session - re-reading
  // monthsParam() per click would let a month-filter change mid-session mix pages fetched under
  // different filters at the same offset.
  const months = monthsParam();
  let page: RecentObservationsPage;
  try {
    page = await getJson("/api/observations/photos", {
      query: { region_id: regionId, months },
    });
  } catch (error) {
    container.innerHTML = `<p class="hint">${escapeHtml(errorDetail(error))}</p>`;
    return false;
  }
  if (!page.observations.length) {
    container.innerHTML = "<p class='hint'>No recent observations here yet.</p>";
    return true;
  }
  container.innerHTML = page.observations.map(renderObsPhoto).join("");
  let offset = page.observations.length;
  let hasMore = page.has_more;
  if (hasMore) {
    const loadMoreButton = document.createElement("button");
    loadMoreButton.type = "button";
    loadMoreButton.className = "show-more";
    loadMoreButton.textContent = "Load more";
    loadMoreButton.onclick = async (e) => {
      e.stopPropagation();
      loadMoreButton.textContent = "Loading…";
      loadMoreButton.disabled = true;
      let nextPage: RecentObservationsPage;
      try {
        nextPage = await getJson("/api/observations/photos", {
          query: { region_id: regionId, months, offset },
        });
      } catch (error) {
        setStatus(errorDetail(error));
        loadMoreButton.disabled = false;
        loadMoreButton.textContent = "Load more";
        return;
      }
      offset += nextPage.observations.length;
      hasMore = nextPage.has_more;
      loadMoreButton.insertAdjacentHTML("beforebegin", nextPage.observations.map(renderObsPhoto).join(""));
      if (hasMore) {
        loadMoreButton.disabled = false;
        loadMoreButton.textContent = "Load more";
      } else {
        loadMoreButton.remove();
      }
    };
    container.appendChild(loadMoreButton);
  }
  return true;
}

// Same fetch-once-per-card pattern as loadCalendarInto/loadPhotosInto. Scoped to the
// destination's own true circle (regionRadiusKm() - the same footprint issue #161 already uses
// for precise-observations, not the whole home search radius) and to trailheads only (`kind`,
// issue #115 follow-up) - a destination card should list what's actually reachable from inside
// it, not every path/route/trailhead in the search area. Selecting a row draws the real trail on
// the map (layers.ts's selectTrailhead) rather than just opening a popup.
export async function loadTrailheadsInto(region: RegionScore, container: HTMLElement): Promise<boolean> {
  container.innerHTML = "<p class='hint'>Loading…</p>";
  let trailheads: Trail[];
  try {
    // See layers.ts's LandUnit cast - `geometry` is real GeoJSON, just untyped on the backend.
    trailheads = (await getJson("/api/trails", {
      query: { region_id: region.region_id, kind: "trailhead", radius_km: regionRadiusKm(), limit: 20 },
    })) as unknown as Trail[];
  } catch (error) {
    container.innerHTML = `<p class="hint">${escapeHtml(errorDetail(error))}</p>`;
    return false;
  }
  if (!trailheads.length) {
    container.innerHTML = "<p class='hint'>No trailheads cached in this destination yet.</p>";
    return true;
  }
  container.innerHTML = "";
  const list = document.createElement("div");
  list.className = "chips";
  // Only one card's trailheads are plotted at a time (plotTrailhead clears the previous set),
  // same as camps/land - opening a different card's Trails tab replaces these, it doesn't add on.
  clearTrailheadMarkers();
  const rows: { button: HTMLButtonElement; marker: L.Marker }[] = [];
  const selectRow = (trailhead: Trail, button: HTMLButtonElement, marker: L.Marker): void => {
    rows.forEach((row) => {
      row.button.classList.remove("active");
      setTrailheadActive(row.marker, false);
    });
    button.classList.add("active");
    setTrailheadActive(marker, true);
    selectTrailhead(trailhead);
  };
  trailheads.forEach((trailhead) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chip";
    button.textContent = `${trailhead.name} · ${dist(trailhead.distance_km)}`;
    const marker = plotTrailhead(trailhead.center_lat, trailhead.center_lng, trailhead.name, () =>
      selectRow(trailhead, button, marker),
    );
    button.onclick = (e) => {
      e.stopPropagation();
      selectRow(trailhead, button, marker);
    };
    rows.push({ button, marker });
    list.appendChild(button);
  });
  container.appendChild(list);
  return true;
}

// Same fetch-once-per-card pattern as loadTrailheadsInto, scoped to the destination's own true
// circle (regionRadiusKm()). Unlike a trailhead, a campsite is already a complete point feature
// (name, fee, coords) - no server-side "resolve the real thing" step, so selecting a row just
// syncs the active chip/marker pair and opens the marker's popup, instead of drawing anything new.
export async function loadCampgroundsInto(region: RegionScore, container: HTMLElement): Promise<boolean> {
  container.innerHTML = "<p class='hint'>Loading…</p>";
  let sites: CampSite[];
  try {
    sites = await getJson("/api/camps", {
      query: { region_id: region.region_id, radius_km: regionRadiusKm(), limit: 20 },
    });
  } catch (error) {
    container.innerHTML = `<p class="hint">${escapeHtml(errorDetail(error))}</p>`;
    return false;
  }
  if (!sites.length) {
    container.innerHTML = "<p class='hint'>No campgrounds cached in this destination yet.</p>";
    return true;
  }
  container.innerHTML = "";
  const list = document.createElement("div");
  list.className = "chips";
  // Only one card's campgrounds are plotted at a time (clearCardCampMarkers below clears the
  // previous set), same as the Trails tab's trailhead markers.
  clearCardCampMarkers();
  const rows: { button: HTMLButtonElement; marker: L.CircleMarker; site: CampSite }[] = [];
  const selectRow = (site: CampSite, button: HTMLButtonElement, marker: L.CircleMarker): void => {
    rows.forEach((row) => {
      row.button.classList.remove("active");
      setCardCampActive(row.marker, row.site, false);
    });
    button.classList.add("active");
    setCardCampActive(marker, site, true);
    marker.openPopup();
  };
  sites.forEach((site) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chip";
    const feeText = feeLabel(site.free === true, site.fee);
    button.textContent = `${site.name} · ${dist(site.distance_km)} · ${feeText}`;
    const marker = plotCardCamp(site, () => selectRow(site, button, marker));
    marker.bindPopup(`<b>${escapeHtml(site.name)}</b><br>${dist(site.distance_km)} · ${escapeHtml(feeText)}`);
    button.onclick = (e) => {
      e.stopPropagation();
      selectRow(site, button, marker);
    };
    rows.push({ button, marker, site });
    list.appendChild(button);
  });
  container.appendChild(list);
  return true;
}
