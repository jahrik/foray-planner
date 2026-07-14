import L from "leaflet";

import { getJson } from "./api/client";
import type { AlertRegion, Calendar, RegionScore } from "./api/types";
import { focusRegion } from "./layers";
import { clearMarkers, HEAT_RGB, map, plot } from "./map";
import { dist, errorDetail, escapeHtml, inatUrl, MONTHS, qs, setStatus, state } from "./state";

export function initMonths(): void {
  const box = qs("#months");
  MONTHS.forEach((label, index) => {
    const month = index + 1;
    const button = document.createElement("button");
    button.textContent = label;
    if (state.months.has(month)) button.classList.add("on");
    button.onclick = () => {
      if (state.months.has(month)) {
        state.months.delete(month);
        button.classList.remove("on");
      } else {
        state.months.add(month);
        button.classList.add("on");
      }
      if (state.view === "destinations") runDestinations();
    };
    box.appendChild(button);
  });
}

export function monthsParam(): string {
  const ordered = [...state.months].sort((left, right) => left - right);
  return ordered.length ? ordered.join(",") : "1,2,3,4,5,6,7,8,9,10,11,12";
}

interface ChipData {
  taxon_id: number;
  common_name: string;
  label?: string;
}

// common_name/label ultimately come from iNaturalist (user-editable), so escape before
// interpolating into an HTML string template.
const speciesChip = (hit: ChipData, extraClass?: string): string =>
  `<a class="chip${extraClass ? " " + extraClass : ""}" href="${inatUrl(hit.taxon_id)}"
      target="_blank" rel="noopener" onclick="event.stopPropagation()"
   >${escapeHtml(hit.common_name)}${hit.label ? " · " + escapeHtml(hit.label) : ""}</a>`;

// Tracks the pending auto-zoom-in timeout so a second runDestinations() call (a fast months
// toggle, a new location) can cancel the previous one - otherwise a stale timeout could fire
// after clearMarkers() has already removed its markers from the map.
let pendingZoomIn: ReturnType<typeof setTimeout> | null = null;

export async function runDestinations(): Promise<void> {
  if (pendingZoomIn !== null) {
    clearTimeout(pendingZoomIn);
    pendingZoomIn = null;
  }
  setStatus("Ranking…");
  clearMarkers();
  let regions: RegionScore[];
  try {
    regions = await getJson<RegionScore[]>(`/api/destinations?months=${monthsParam()}`);
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  const panel = qs("#panel");
  if (!regions.length) {
    panel.innerHTML =
      "<p class='hint'>No regions in range for those months. Try widening months or running Refresh.</p>";
    setStatus("");
    return;
  }
  // Rank list is the only thing in the panel now - each card's calendar lives behind a tab
  // inside that card (see below) instead of a shared slot above the list, so picking a region
  // no longer reshuffles what's on screen above it.
  panel.innerHTML = `<div id="rank-list"></div>`;
  const rankList = qs("#rank-list");
  const markers = regions.map((region, rank) => {
    const marker = plot(region.center_lat, region.center_lng, region.score_norm, region.recent_count > 0);
    const card = document.createElement("div");
    card.className = "rank";
    card.innerHTML = `
      <h3><span>#${rank + 1} · ${dist(region.distance_km)}</span><span>${region.n_species} spp</span></h3>
      <div class="bar"><span style="width:${(region.score_norm * 100).toFixed(0)}%"></span></div>
      <div class="meta">score ${region.score_norm.toFixed(2)}${region.recent_count ? ` · ${region.recent_count} seen recently` : ""}</div>
      <div class="rank-tabs">
        <button type="button" class="rank-tab active" data-tab="species">Species</button>
        <button type="button" class="rank-tab" data-tab="calendar">Calendar</button>
      </div>
      <div class="chips" data-tab-content="species">${region.species
        .slice(0, 6)
        .map((hit) => speciesChip({ ...hit, label: (hit.w_pheno * 100).toFixed(0) + "%" }))
        .join("")}</div>
      <div class="rank-calendar" data-tab-content="calendar" style="display:none"></div>`;
    const speciesTab = card.querySelector<HTMLButtonElement>('[data-tab="species"]')!;
    const calendarTab = card.querySelector<HTMLButtonElement>('[data-tab="calendar"]')!;
    const speciesBody = card.querySelector<HTMLElement>('[data-tab-content="species"]')!;
    const calendarBody = card.querySelector<HTMLElement>('[data-tab-content="calendar"]')!;
    let calendarLoaded = false;
    const showTab = (tab: "species" | "calendar") => {
      speciesTab.classList.toggle("active", tab === "species");
      calendarTab.classList.toggle("active", tab === "calendar");
      speciesBody.style.display = tab === "species" ? "" : "none";
      calendarBody.style.display = tab === "calendar" ? "" : "none";
    };
    speciesTab.onclick = (e) => {
      e.stopPropagation();
      showTab("species");
    };
    calendarTab.onclick = (e) => {
      e.stopPropagation();
      showTab("calendar");
      if (!calendarLoaded) {
        calendarLoaded = true;
        loadCalendarInto(region.region_id, calendarBody);
      }
    };
    // Selecting a region - from either its card or its map marker - highlights the card and
    // scrolls it into view instead of popping a bubble over the marker (which covered up the
    // very thing you were trying to look at). The card already shows everything the popup used to.
    const selectCard = () => {
      rankList.querySelectorAll(".rank").forEach((el) => el.classList.remove("active"));
      card.classList.add("active");
      card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    };
    card.onclick = () => {
      map.setView([region.center_lat, region.center_lng], 9);
      focusRegion(region.center_lat, region.center_lng);
      selectCard();
    };
    marker.on("click", () => {
      focusRegion(region.center_lat, region.center_lng);
      selectCard();
    });
    rankList.appendChild(card);
    return marker;
  });
  setStatus(`${regions.length} regions`);

  // Automate the zoom + layer load for the (already server-sorted) top result: fit the map to
  // the full spread first ("zoom out"), then fly into the best destination and load its
  // trails/camps/land - the same thing a click on the #1 card already does. Its calendar loads
  // on demand from the Calendar tab, same as every other card.
  const top = regions[0];
  if (regions.length > 1) {
    map.fitBounds(L.latLngBounds(markers.map((marker) => marker.getLatLng())), {
      padding: [40, 40],
      maxZoom: 9,
    });
    pendingZoomIn = window.setTimeout(() => {
      pendingZoomIn = null;
      if (state.view !== "destinations") return; // user navigated away while we waited
      map.flyTo([top.center_lat, top.center_lng], 9);
    }, 900);
  } else {
    map.flyTo([top.center_lat, top.center_lng], 9);
  }
  focusRegion(top.center_lat, top.center_lng);
  rankList.querySelector(".rank")?.classList.add("active");
}

// Fetches once per card (cached by the `calendarLoaded` flag at the call site) and renders
// straight into that card's own calendar-tab body, rather than a slot shared across all cards.
async function loadCalendarInto(regionId: string, container: HTMLElement): Promise<void> {
  container.innerHTML = "<p class='hint'>Loading…</p>";
  let calendar: Calendar;
  try {
    calendar = await getJson<Calendar>(`/api/calendar?region_id=${regionId}`);
  } catch (error) {
    container.innerHTML = `<p class="hint">${escapeHtml(errorDetail(error))}</p>`;
    return;
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
}

export async function runAlerts(): Promise<void> {
  setStatus("Checking recent activity…");
  clearMarkers();
  let regions: AlertRegion[];
  try {
    regions = await getJson<AlertRegion[]>("/api/alerts");
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  const panel = qs("#panel");
  if (!regions.length) {
    panel.innerHTML = "<p class='hint'>No target species observed in the trailing window yet.</p>";
    setStatus("");
    return;
  }
  panel.innerHTML = "<h3 style='margin-top:0'>Fruiting now / recently</h3>";
  regions.forEach((region) => {
    const marker = plot(region.center_lat, region.center_lng, Math.min(1, region.total / 10), true);
    const card = document.createElement("div");
    card.className = "rank";

    const placeText = region.species[0]?.place_guess
      ? ` · ${escapeHtml(region.species[0].place_guess)}`
      : "";
    card.innerHTML = `<h3><span>${dist(region.distance_km)}${placeText}</span><span>${region.total} recent</span></h3>
      <div class="chips">${region.species
        .map((hit) => {
          const label = hit.count + " · " + hit.last_seen + (hit.obscured ? " ⚠ fuzzy" : "");
          const safeUri = hit.uri?.startsWith("https://") ? hit.uri : null;
          if (safeUri) {
            return `<a class="chip live" href="${escapeHtml(safeUri)}"
              target="_blank" rel="noopener" onclick="event.stopPropagation()"
              >${escapeHtml(hit.common_name)} · ${escapeHtml(label)}</a>`;
          }
          return speciesChip({ ...hit, label }, "live");
        })
        .join("")}</div>`;
    const selectCard = () => {
      panel.querySelectorAll(".rank").forEach((el) => el.classList.remove("active"));
      card.classList.add("active");
      card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    };
    card.onclick = () => {
      map.setView([region.center_lat, region.center_lng], 9);
      focusRegion(region.center_lat, region.center_lng);
      selectCard();
    };
    marker.on("click", () => {
      focusRegion(region.center_lat, region.center_lng);
      selectCard();
    });
    panel.appendChild(card);
  });
  setStatus(`${regions.length} active regions`);
}
