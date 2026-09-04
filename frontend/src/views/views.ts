import { getJson } from "../api/client";
import type { RegionPlace, RegionScore } from "../api/types";
import { createCardSelection, createRunGuard } from "../ui/card-select";
import { makeActivatable, speciesChip, stopLinkPropagation } from "../ui/card-dom";
import {
  loadCalendarInto,
  loadCampgroundsInto,
  loadPhotosInto,
  loadTrailheadsInto,
} from "./destination-tabs";
import { focusRegion } from "../map/layers";
import { createLazyLoader } from "../ui/lazy-panel";
import { focusOnMap, sheetEnabled, snapTo } from "../map/sheet";
import { clearMarkers, map, plot } from "../map/map";
import {
  dist,
  elevationLabel,
  errorDetail,
  fireBadges,
  monthsParam,
  MONTHS,
  qs,
  rainMeta,
  setStatus,
  state,
} from "../state";

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
const destinationsGuard = createRunGuard("destinations");

export async function runDestinations(): Promise<void> {
  const isCurrent = destinationsGuard.begin();
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
    if (!isCurrent()) return;
    setStatus(errorDetail(error));
    return;
  }
  if (!isCurrent()) return;
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
  const cardSelection = createCardSelection(rankList);
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
      <div class="meta">score <span class="num">${region.score_norm.toFixed(2)}</span> · <span class="num">${region.n_species}</span> spp · ${region.recent_count ? `<span class="num">${region.recent_count}</span> recent` : "no recent obs"}${region.elevation_m != null ? ` · elev <span class="num">${elevationLabel(region.elevation_m)}</span>` : ""}${rainMeta(region)}</div>
      ${fireBadges(region.fire_nearby)}

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
    // Each detail tab fetches once, on first open (createLazyLoader owns the idle/loading/loaded
    // guard and the retry-on-failure reset).
    const calendarLoader = createLazyLoader(() => loadCalendarInto(region.region_id, calendarBody));
    const photosLoader = createLazyLoader(() => loadPhotosInto(region.region_id, photosBody));
    const trailsLoader = createLazyLoader(() => loadTrailheadsInto(region, trailsBody));
    const campsLoader = createLazyLoader(() => loadCampgroundsInto(region, campsBody));
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
      calendarLoader.open();
    };
    photosTab.onclick = (e) => {
      e.stopPropagation();
      showTab("photos");
      photosLoader.open();
    };
    trailsTab.onclick = (e) => {
      e.stopPropagation();
      showTab("trails");
      trailsLoader.open();
    };
    campsTab.onclick = (e) => {
      e.stopPropagation();
      showTab("camps");
      campsLoader.open();
    };
    // Selecting a region - from either its card or its map marker - highlights the card and
    // scrolls it into view instead of popping a bubble over the marker (which covered up the
    // very thing you were trying to look at). The card already shows everything the popup used to.
    // Its marker also snaps to its true cell-footprint size (see selectSize in map.ts), with the
    // previously selected marker (if any) reverting to its score-scaled preview size.
    const selectCard = () => cardSelection.select(card, marker);
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
  // is auto-selected (focus + highlighted card) like a click on the #1 card; each card's detail
  // tabs still load on demand when opened.
  const top = regions[0];
  const topMarker = markers[0];
  const topCard = rankList.querySelector<HTMLElement>(".rank");
  if (top && topMarker && topCard) {
    focusRegion(top.center_lat, top.center_lng);
    cardSelection.selectInitial(topCard, topMarker);
  }

  // Card titles start as rank + distance only; each card's notable-place name (issue #206)
  // loads afterward, one region at a time rather than all N in parallel - Nominatim's usage
  // policy caps requests at ~1/s (the backend throttles too, see geocode._throttle, but no
  // sense firing a burst of requests this run will just make the backend queue up anyway).
  // Cached regions (region_places) resolve near-instantly, so this only visibly staggers on a
  // cold cache. Fire-and-forget: runDestinations() itself doesn't wait on card titles.
  void (async () => {
    for (const { region, rank, numSpan } of titleTargets) {
      if (!isCurrent()) return;
      let place: RegionPlace;
      try {
        place = await getJson("/api/destinations/{region_id}/place", {
          path: { region_id: region.region_id },
        });
      } catch {
        continue; // best-effort - leave this card's title as rank + distance
      }
      if (!isCurrent()) return;
      if (place.place_name) {
        numSpan.textContent = `#${rank + 1} · ${place.place_name} · ${dist(region.distance_km)}`;
      }
    }
  })();
}
