import L from "leaflet";

import { getJson } from "./api/client";
import type {
  AlertRegion,
  Calendar,
  CampSite,
  RecentObservation,
  RecentObservationsPage,
  RegionPlace,
  RegionScore,
  Trail,
} from "./api/types";
import { focusRegion, selectTrailhead } from "./layers";
import { focusOnMap, sheetEnabled, snapTo } from "./sheet";
import {
  clearCardCampMarkers,
  clearMarkers,
  clearTrailheadMarkers,
  deselectSize,
  HEAT_RGB,
  map,
  plot,
  plotCardCamp,
  plotTrailhead,
  regionRadiusKm,
  selectSize,
  setCardCampActive,
  setTrailheadActive,
} from "./map";
import {
  dist,
  elevationLabel,
  displayName,
  errorDetail,
  escapeHtml,
  inatUrl,
  monthsParam,
  MONTHS,
  qs,
  setStatus,
  state,
} from "./state";

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

// Links nested inside an activatable card (species/photo chips) need their clicks to open
// normally without also firing the card's own activate handler. An inline onclick attribute
// can't do this - the CSP (script-src 'self', no unsafe-inline) blocks it outright - so this
// delegates from the link's container instead, which stops the click before it ever bubbles
// to the card.
function stopLinkPropagation(container: HTMLElement): void {
  container.addEventListener("click", (event) => {
    if (event.target instanceof Element && event.target.closest("a")) event.stopPropagation();
  });
}

export function initMonths(): void {
  const box = qs("#months");
  MONTHS.forEach((label, index) => {
    const month = index + 1;
    const button = document.createElement("button");
    button.textContent = label;
    button.setAttribute("aria-pressed", String(state.months.has(month)));
    if (state.months.has(month)) button.classList.add("on");
    button.onclick = () => {
      if (state.months.has(month)) {
        state.months.delete(month);
        button.classList.remove("on");
      } else {
        state.months.add(month);
        button.classList.add("on");
      }
      button.setAttribute("aria-pressed", String(state.months.has(month)));
      if (state.view === "destinations") runDestinations();
    };
    box.appendChild(button);
  });
}

interface ChipData {
  taxon_id: number;
  name: string;
  common_name?: string | null;
  label?: string;
  title?: string;
}

// name/common_name/label ultimately come from iNaturalist (user-editable), so escape before
// interpolating into an HTML string template.
const speciesChip = (hit: ChipData, extraClass?: string): string =>
  `<a class="chip${extraClass ? " " + extraClass : ""}" href="${inatUrl(hit.taxon_id)}"
      target="_blank" rel="noopener"${hit.title ? ` title="${escapeHtml(hit.title)}"` : ""}
   >${escapeHtml(displayName(hit))}${hit.label ? " · " + escapeHtml(hit.label) : ""}</a>`;

// w_pheno is "share of this genus's regional sightings that fall in the selected month(s)" -
// a seasonality/in-season indicator, not a find-probability or share-of-destination figure
// (issue #172). Spelled out in a tooltip since the bare "%" chip label is otherwise ambiguous.
// With no months toggled, monthsParam() falls back to all 12 (see monthsParam above) - the
// "selected month(s)" then covers the whole year, so w_pheno is trivially 100% for every genus
// and month_count is really its all-time total, not an in-season subset. Wording has to
// reflect that or the tooltip claims a seasonality signal that isn't actually being computed.
const phenoTitle = (pct: number, monthCount: number, allMonths: boolean): string =>
  allMonths
    ? `No month filter applied, so this is just this genus's all-time sighting count here (${monthCount}) - the % is always 100% with every month selected and isn't a seasonality signal in this mode.`
    : `${pct}% of this genus's research-grade sightings here fall in your selected month(s) - a seasonality ` +
      `indicator, not a chance of finding it. Chips are ordered by the ${monthCount} in-season sighting${monthCount === 1 ? "" : "s"} shown next to the percentage, not by the percentage itself.`;

// Guards against overlapping runDestinations() calls stepping on each other - e.g. main.ts's
// startup sequence can fire one on the head-start timeout and a second once geolocation actually
// resolves. If the first call's fetch happens to resolve after the second has already rendered,
// it would otherwise plot its own (stale) markers on top without re-clearing the map first (its
// own clearMarkers() already ran, before either fetch started) - duplicate destination circles
// that never go away until the next run. A stale call bails out entirely once it notices a newer
// one has started, rather than touching the panel or the map at all.
let destinationsRunToken = 0;

export async function runDestinations(): Promise<void> {
  const token = ++destinationsRunToken;
  setStatus("Ranking…");
  clearMarkers();
  // No months toggled reads the same as all 12 toggled (monthsParam()'s fallback) - either way
  // there's no actual month restriction, so the phenology chip's %/tooltip need different wording.
  const allMonths = state.months.size === 0 || state.months.size === 12;
  let regions: RegionScore[];
  try {
    regions = await getJson("/api/destinations", { query: { months: monthsParam() } });
  } catch (error) {
    // Superseded either by a newer runDestinations() call or by the user switching away from
    // the Destinations tab entirely while this fetch was in flight - either way, whoever owns
    // #panel/the map now shouldn't have their state clobbered by a stale response.
    if (token !== destinationsRunToken || state.view !== "destinations") return;
    setStatus(errorDetail(error));
    return;
  }
  if (token !== destinationsRunToken || state.view !== "destinations") return;
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
  // Each card's place-name lookup (issue #206) runs after the initial render, not inline in
  // this map() - see the sequential loop below.
  const titleTargets: { region: RegionScore; rank: number; numSpan: HTMLElement }[] = [];
  const markers = regions.map((region, rank) => {
    const marker = plot(region.center_lat, region.center_lng, region.score_norm, region.recent_count > 0);
    const card = document.createElement("div");
    card.className = "rank";
    card.innerHTML = `
      <h3><span class="num">#${rank + 1} · ${dist(region.distance_km)}</span></h3>
      <div class="bar"><span style="width:${(region.score_norm * 100).toFixed(0)}%"></span></div>
      <div class="meta">score <span class="num">${region.score_norm.toFixed(2)}</span> · <span class="num">${region.n_species}</span> spp · ${region.recent_count ? `<span class="num">${region.recent_count}</span> recent` : "no recent obs"}${region.elevation_m != null ? ` · elev <span class="num">${elevationLabel(region.elevation_m)}</span>` : ""}</div>
      <div class="rank-tabs">
        <button type="button" class="rank-tab active" data-tab="species">Species</button>
        <button type="button" class="rank-tab" data-tab="calendar">Calendar</button>
        <button type="button" class="rank-tab" data-tab="photos">Photos</button>
        <button type="button" class="rank-tab" data-tab="trails">Trails</button>
        <button type="button" class="rank-tab" data-tab="camps">Campgrounds</button>
      </div>
      <div data-tab-content="species">
        <div class="chips">${region.species
          .slice(0, 6)
          .map((hit) => {
            const pct = Math.round(hit.w_pheno * 100);
            return speciesChip({
              ...hit,
              label: `${pct}% · ${hit.month_count}`,
              title: phenoTitle(pct, hit.month_count, allMonths),
            });
          })
          .join("")}</div>
        ${
          region.species.length > 6
            ? `<button type="button" class="show-more" aria-expanded="false">Show all ${region.species.length}</button>`
            : ""
        }
      </div>
      <div class="rank-calendar" data-tab-content="calendar" style="display:none"></div>
      <div class="rank-photos" data-tab-content="photos" style="display:none"></div>
      <div class="rank-trails" data-tab-content="trails" style="display:none"></div>
      <div class="rank-camps" data-tab-content="camps" style="display:none"></div>`;
    titleTargets.push({ region, rank, numSpan: qs<HTMLElement>(".num", card) });
    const speciesTab = qs<HTMLButtonElement>('[data-tab="species"]', card);
    const calendarTab = qs<HTMLButtonElement>('[data-tab="calendar"]', card);
    const photosTab = qs<HTMLButtonElement>('[data-tab="photos"]', card);
    const trailsTab = qs<HTMLButtonElement>('[data-tab="trails"]', card);
    const campsTab = qs<HTMLButtonElement>('[data-tab="camps"]', card);
    const speciesBody = qs<HTMLElement>('[data-tab-content="species"]', card);
    const calendarBody = qs<HTMLElement>('[data-tab-content="calendar"]', card);
    const photosBody = qs<HTMLElement>('[data-tab-content="photos"]', card);
    const trailsBody = qs<HTMLElement>('[data-tab-content="trails"]', card);
    const campsBody = qs<HTMLElement>('[data-tab-content="camps"]', card);
    stopLinkPropagation(speciesBody);
    stopLinkPropagation(photosBody);
    stopLinkPropagation(trailsBody);
    stopLinkPropagation(campsBody);
    const chipsContainer = qs<HTMLElement>(".chips", speciesBody);
    const showMoreButton = card.querySelector<HTMLButtonElement>(".show-more");
    if (showMoreButton) {
      let expanded = false;
      showMoreButton.onclick = (e) => {
        e.stopPropagation();
        expanded = !expanded;
        chipsContainer.innerHTML = region.species
          .slice(0, expanded ? undefined : 6)
          .map((hit) => {
            const pct = Math.round(hit.w_pheno * 100);
            return speciesChip({
              ...hit,
              label: `${pct}% · ${hit.month_count}`,
              title: phenoTitle(pct, hit.month_count, allMonths),
            });
          })
          .join("");
        showMoreButton.textContent = expanded ? "Show less" : `Show all ${region.species.length}`;
        showMoreButton.setAttribute("aria-expanded", String(expanded));
      };
    }
    // "loading" (not just a boolean) guards against a second click firing a duplicate fetch
    // while the first is still in flight; a failed fetch resets to "idle" so the tab can be
    // retried, rather than permanently disabling it like a plain "already loaded" flag would.
    let calendarState: "idle" | "loading" | "loaded" = "idle";
    let photosState: "idle" | "loading" | "loaded" = "idle";
    let trailsState: "idle" | "loading" | "loaded" = "idle";
    let campsState: "idle" | "loading" | "loaded" = "idle";
    const showTab = (tab: "species" | "calendar" | "photos" | "trails" | "camps") => {
      speciesTab.classList.toggle("active", tab === "species");
      calendarTab.classList.toggle("active", tab === "calendar");
      photosTab.classList.toggle("active", tab === "photos");
      trailsTab.classList.toggle("active", tab === "trails");
      campsTab.classList.toggle("active", tab === "camps");
      speciesBody.style.display = tab === "species" ? "" : "none";
      calendarBody.style.display = tab === "calendar" ? "" : "none";
      photosBody.style.display = tab === "photos" ? "" : "none";
      trailsBody.style.display = tab === "trails" ? "" : "none";
      campsBody.style.display = tab === "camps" ? "" : "none";
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
    trailsTab.onclick = (e) => {
      e.stopPropagation();
      showTab("trails");
      if (trailsState === "idle") {
        trailsState = "loading";
        loadTrailheadsInto(region, trailsBody).then((succeeded) => {
          trailsState = succeeded ? "loaded" : "idle";
        });
      }
    };
    campsTab.onclick = (e) => {
      e.stopPropagation();
      showTab("camps");
      if (campsState === "idle") {
        campsState = "loading";
        loadCampgroundsInto(region, campsBody).then((succeeded) => {
          campsState = succeeded ? "loaded" : "idle";
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
      snapTo("full"); // opening a card's detail expands the mobile sheet
      focusOnMap(region.center_lat, region.center_lng, 9);
      focusRegion(region.center_lat, region.center_lng);
      selectCard();
    });
    // Opening any inner tab (Calendar/Trails/Campgrounds/…) is a detail view -> expand the sheet.
    // Capture phase: the per-tab handlers call stopPropagation(), so a bubble listener here
    // would never see the click.
    qs<HTMLElement>(".rank-tabs", card).addEventListener("click", () => snapTo("full"), true);
    marker.on("click", () => {
      if (sheetEnabled()) {
        snapTo("half"); // a map-pin tap raises the sheet to its middle detent
        focusOnMap(region.center_lat, region.center_lng, map.getZoom()); // offset clear of the sheet
      }
      focusRegion(region.center_lat, region.center_lng);
      selectCard();
    });
    rankList.appendChild(card);
    return marker;
  });
  setStatus(`${regions.length} regions`);

  // No auto-zoom/pan on results - the map stays wherever the user has it (centered on their
  // location by default) and they zoom/pan themselves. The (already server-sorted) top result
  // still gets its trails/camps/land auto-loaded, same as a click on the #1 card; its calendar
  // loads on demand from the Calendar tab like every other card.
  const top = regions[0];
  const topMarker = markers[0];
  if (top && topMarker) {
    focusRegion(top.center_lat, top.center_lng);
    rankList.querySelector(".rank")?.classList.add("active");
    selectSize(topMarker);
    selected = { marker: topMarker, weight: top.score_norm };
  }

  // Card titles start as rank + distance only; each card's notable-place name (issue #206)
  // loads afterward, one region at a time rather than all N in parallel - Nominatim's usage
  // policy caps requests at ~1/s (the backend throttles too, see geocode._throttle, but no
  // sense firing a burst of requests this run will just make the backend queue up anyway).
  // Cached regions (region_places) resolve near-instantly, so this only visibly staggers on a
  // cold cache. Fire-and-forget: runDestinations() itself doesn't wait on card titles.
  void (async () => {
    for (const { region, rank, numSpan } of titleTargets) {
      if (token !== destinationsRunToken || state.view !== "destinations") return;
      let place: RegionPlace;
      try {
        place = await getJson("/api/destinations/{region_id}/place", {
          path: { region_id: region.region_id },
        });
      } catch {
        continue; // best-effort - leave this card's title as rank + distance
      }
      if (token !== destinationsRunToken || state.view !== "destinations") return;
      if (place.place_name) {
        numSpan.textContent = `#${rank + 1} · ${place.place_name} · ${dist(region.distance_km)}`;
      }
    }
  })();
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
async function loadPhotosInto(regionId: string, container: HTMLElement): Promise<boolean> {
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
async function loadTrailheadsInto(region: RegionScore, container: HTMLElement): Promise<boolean> {
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
async function loadCampgroundsInto(region: RegionScore, container: HTMLElement): Promise<boolean> {
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
    const feeLabel = site.free === true ? "free" : site.fee ? site.fee : "cost unknown";
    button.textContent = `${site.name} · ${dist(site.distance_km)} · ${feeLabel}`;
    const marker = plotCardCamp(site, () => selectRow(site, button, marker));
    marker.bindPopup(
      `<b>${escapeHtml(site.name)}</b><br>${dist(site.distance_km)} · ${escapeHtml(feeLabel)}`,
    );
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

// Same overlapping-call guard as destinationsRunToken above - runAlerts() can also be triggered
// more than once in flight (tab switches, refreshCurrentView() calls). Also bails if the user has
// since switched away from the Alerts tab entirely while the fetch was in flight, not just if a
// newer runAlerts() call superseded this one.
let alertsRunToken = 0;

export async function runAlerts(): Promise<void> {
  const token = ++alertsRunToken;
  setStatus("Checking recent activity…");
  clearMarkers();
  let regions: AlertRegion[];
  try {
    regions = await getJson("/api/alerts");
  } catch (error) {
    if (token !== alertsRunToken || state.view !== "alerts") return;
    setStatus(errorDetail(error));
    return;
  }
  if (token !== alertsRunToken || state.view !== "alerts") return;
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

    const placeText = region.species[0]?.place_guess ? ` · ${escapeHtml(region.species[0].place_guess)}` : "";
    card.innerHTML = `<h3><span><span class="num">${dist(region.distance_km)}</span>${placeText}</span><span class="num">${region.total} recent</span></h3>
      <div class="chips">${region.species
        .map((hit) => {
          const label = hit.count + " · " + hit.last_seen + (hit.obscured ? " ⚠ fuzzy" : "");
          const safeUri = hit.uri?.startsWith("https://") ? hit.uri : null;
          if (safeUri) {
            return `<a class="chip live" href="${escapeHtml(safeUri)}"
              target="_blank" rel="noopener"
              >${escapeHtml(displayName(hit))} · ${escapeHtml(label)}</a>`;
          }
          return speciesChip({ ...hit, label }, "live");
        })
        .join("")}</div>`;
    stopLinkPropagation(qs<HTMLElement>(".chips", card));
    const selectCard = () => {
      panel.querySelectorAll(".rank").forEach((el) => el.classList.remove("active"));
      card.classList.add("active");
      card.scrollIntoView({ block: "nearest", behavior: "smooth" });
      if (selected && selected.marker !== marker) deselectSize(selected.marker, selected.weight);
      selectSize(marker);
      selected = { marker, weight };
    };
    makeActivatable(card, () => {
      snapTo("full");
      focusOnMap(region.center_lat, region.center_lng, 9);
      focusRegion(region.center_lat, region.center_lng);
      selectCard();
    });
    marker.on("click", () => {
      if (sheetEnabled()) {
        snapTo("half");
        focusOnMap(region.center_lat, region.center_lng, map.getZoom());
      }
      focusRegion(region.center_lat, region.center_lng);
      selectCard();
    });
    panel.appendChild(card);
  });
  setStatus(`${regions.length} active regions`);
}
