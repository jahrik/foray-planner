import L from "leaflet";

import { getJson } from "./api/client";
import type { AlertRegion, Calendar, RecentObservation, RegionScore } from "./api/types";
import { focusRegion } from "./layers";
import { clearMarkers, deselectSize, HEAT_RGB, map, plot, selectSize } from "./map";
import { dist, errorDetail, escapeHtml, inatUrl, MONTHS, qs, setStatus, state } from "./state";

// Cards act as buttons (selecting a region) but are plain <div>s for layout flexibility, so make
// them keyboard-operable: focusable, and Enter/Space activates - but only when the key event's
// target is the card itself, not a nested button/link (those already get native keyboard
// activation, and re-triggering the card on top of that would double-fire).
function makeActivatable(card: HTMLElement, activate: () => void): void {
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  card.onclick = activate;
  card.onkeydown = (e) => {
    if (e.target !== card) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      activate();
    }
  };
}

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

export async function runDestinations(): Promise<void> {
  setStatus("Ranking…");
  clearMarkers();
  let regions: RegionScore[];
  try {
    regions = await getJson("/api/destinations", { query: { months: monthsParam() } });
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
  // Only one region's marker shows its true real-world size at a time; selecting a new one
  // reverts whichever marker held that spot back to its score-scaled preview size.
  let selected: { marker: L.Circle; weight: number } | null = null;
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
        <button type="button" class="rank-tab" data-tab="photos">Photos</button>
      </div>
      <div class="chips" data-tab-content="species">${region.species
        .slice(0, 6)
        .map((hit) => speciesChip({ ...hit, label: (hit.w_pheno * 100).toFixed(0) + "%" }))
        .join("")}</div>
      <div class="rank-calendar" data-tab-content="calendar" style="display:none"></div>
      <div class="rank-photos" data-tab-content="photos" style="display:none"></div>`;
    const speciesTab = card.querySelector<HTMLButtonElement>('[data-tab="species"]')!;
    const calendarTab = card.querySelector<HTMLButtonElement>('[data-tab="calendar"]')!;
    const photosTab = card.querySelector<HTMLButtonElement>('[data-tab="photos"]')!;
    const speciesBody = card.querySelector<HTMLElement>('[data-tab-content="species"]')!;
    const calendarBody = card.querySelector<HTMLElement>('[data-tab-content="calendar"]')!;
    const photosBody = card.querySelector<HTMLElement>('[data-tab-content="photos"]')!;
    // "loading" (not just a boolean) guards against a second click firing a duplicate fetch
    // while the first is still in flight; a failed fetch resets to "idle" so the tab can be
    // retried, rather than permanently disabling it like a plain "already loaded" flag would.
    let calendarState: "idle" | "loading" | "loaded" = "idle";
    let photosState: "idle" | "loading" | "loaded" = "idle";
    const showTab = (tab: "species" | "calendar" | "photos") => {
      speciesTab.classList.toggle("active", tab === "species");
      calendarTab.classList.toggle("active", tab === "calendar");
      photosTab.classList.toggle("active", tab === "photos");
      speciesBody.style.display = tab === "species" ? "" : "none";
      calendarBody.style.display = tab === "calendar" ? "" : "none";
      photosBody.style.display = tab === "photos" ? "" : "none";
    };
    speciesTab.onclick = (e) => {
      e.stopPropagation();
      showTab("species");
    };
    calendarTab.onclick = (e) => {
      e.stopPropagation();
      showTab("calendar");
      if (calendarState === "idle") {
        calendarState = "loading";
        loadCalendarInto(region.region_id, calendarBody).then((succeeded) => {
          calendarState = succeeded ? "loaded" : "idle";
        });
      }
    };
    photosTab.onclick = (e) => {
      e.stopPropagation();
      showTab("photos");
      if (photosState === "idle") {
        photosState = "loading";
        loadPhotosInto(region.region_id, photosBody).then((succeeded) => {
          photosState = succeeded ? "loaded" : "idle";
        });
      }
    };
    // Selecting a region - from either its card or its map marker - highlights the card and
    // scrolls it into view instead of popping a bubble over the marker (which covered up the
    // very thing you were trying to look at). The card already shows everything the popup used to.
    // Its marker also snaps to its true cell-footprint size (see selectSize in map.ts), with the
    // previously selected marker (if any) reverting to its score-scaled preview size.
    const selectCard = () => {
      rankList.querySelectorAll(".rank").forEach((el) => el.classList.remove("active"));
      card.classList.add("active");
      card.scrollIntoView({ block: "nearest", behavior: "smooth" });
      if (selected && selected.marker !== marker) deselectSize(selected.marker, selected.weight);
      selectSize(marker);
      selected = { marker, weight: region.score_norm };
    };
    makeActivatable(card, () => {
      map.setView([region.center_lat, region.center_lng], 9);
      focusRegion(region.center_lat, region.center_lng);
      selectCard();
    });
    marker.on("click", () => {
      focusRegion(region.center_lat, region.center_lng);
      selectCard();
    });
    rankList.appendChild(card);
    return marker;
  });
  setStatus(`${regions.length} regions`);

  // Fit the map to every ranked result and stop there - no follow-up zoom into the top pick,
  // which felt disorienting (the map settles, then yanks in tight a moment later). The
  // (already server-sorted) top result still gets its trails/camps/land auto-loaded, same as
  // a click on the #1 card; its calendar loads on demand from the Calendar tab like every
  // other card.
  const top = regions[0];
  map.fitBounds(L.latLngBounds(markers.map((marker) => marker.getLatLng())), {
    padding: [40, 40],
    maxZoom: 9,
  });
  focusRegion(top.center_lat, top.center_lng);
  rankList.querySelector(".rank")?.classList.add("active");
  selectSize(markers[0]);
  selected = { marker: markers[0], weight: top.score_norm };
}

// Fetches once per card (cached by the calendarState flag at the call site) and renders straight
// into that card's own calendar-tab body, rather than a slot shared across all cards. Returns
// whether it succeeded so the caller can tell a real load from a failed one and allow a retry.
async function loadCalendarInto(regionId: string, container: HTMLElement): Promise<boolean> {
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

// Same fetch-once-per-card pattern as loadCalendarInto. Observations without an eligible
// (redisplayable) photo still get listed as a plain link back to iNat, per the license allow-list
// the backend already applied.
async function loadPhotosInto(regionId: string, container: HTMLElement): Promise<boolean> {
  container.innerHTML = "<p class='hint'>Loading…</p>";
  let observations: RecentObservation[];
  try {
    observations = await getJson("/api/observations/photos", { query: { region_id: regionId } });
  } catch (error) {
    container.innerHTML = `<p class="hint">${escapeHtml(errorDetail(error))}</p>`;
    return false;
  }
  if (!observations.length) {
    container.innerHTML = "<p class='hint'>No recent observations here yet.</p>";
    return true;
  }
  container.innerHTML = observations
    .map((obs) => {
      const uri = obs.uri && obs.uri.startsWith("https://") ? escapeHtml(obs.uri) : null;
      const link = uri
        ? `<a href="${uri}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${escapeHtml(obs.common_name)}</a>`
        : escapeHtml(obs.common_name);
      const when = obs.observed_on ? escapeHtml(obs.observed_on) : "";
      const photo = obs.photos[0] && obs.photos[0].url.startsWith("https://") ? obs.photos[0] : null;
      const img = photo
        ? `<img class="obs-thumb" src="${escapeHtml(photo.url)}" alt="${escapeHtml(obs.common_name)}" loading="lazy" />`
        : "";
      const thumb = photo
        ? `${
            uri
              ? `<a href="${uri}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${img}</a>`
              : img
          }
           <div class="meta">${escapeHtml(photo.attribution)}</div>`
        : "";
      return `<div class="obs-photo">${thumb}<div class="meta">${link} · ${when}</div></div>`;
    })
    .join("");
  return true;
}

export async function runAlerts(): Promise<void> {
  setStatus("Checking recent activity…");
  clearMarkers();
  let regions: AlertRegion[];
  try {
    regions = await getJson("/api/alerts");
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
  let selected: { marker: L.Circle; weight: number } | null = null;
  regions.forEach((region) => {
    const weight = Math.min(1, region.total / 10);
    const marker = plot(region.center_lat, region.center_lng, weight, true);
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
      if (selected && selected.marker !== marker) deselectSize(selected.marker, selected.weight);
      selectSize(marker);
      selected = { marker, weight };
    };
    makeActivatable(card, () => {
      map.setView([region.center_lat, region.center_lng], 9);
      focusRegion(region.center_lat, region.center_lng);
      selectCard();
    });
    marker.on("click", () => {
      focusRegion(region.center_lat, region.center_lng);
      selectCard();
    });
    panel.appendChild(card);
  });
  setStatus(`${regions.length} active regions`);
}
