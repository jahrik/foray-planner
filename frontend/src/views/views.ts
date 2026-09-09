import type { LeafletMouseEvent } from "leaflet";

import { getJson } from "../api/client";
import type { AlertRegion, RegionPlace, RegionScore } from "../api/types";
import { escapeHtml } from "../format";
import { createCardSelection, createRunGuard } from "../ui/card-select";
import { buildResultCard, speciesChip, type ResultCardModel } from "../ui/card-dom";
import { whySentence } from "../ui/why";
import { sortRegions } from "./sort";
import { openDetails } from "./details";
import { inShortlist, toggleShortlist } from "./shortlist";
import { focusRegion } from "../map/layers";
import { setMonths } from "../prefs";
import { focusOnMap, sheetEnabled, snapTo } from "../map/sheet";
import { clearMarkers, inspectRoadAt, map, plot } from "../map/map";
import {
  dist,
  displayName,
  elevationLabel,
  errorDetail,
  fireBadges,
  monthsParam,
  MONTHS,
  onScopeChange,
  qs,
  rainMeta,
  setStatus,
  state,
} from "../state";

type PlottedMarker = ReturnType<typeof plot>;

interface CardSpec {
  regionId: string;
  lat: number;
  lng: number;
  live: boolean;
  plotWeight: number;
  model: ResultCardModel;
}

interface CardContext {
  cardSelection: ReturnType<typeof createCardSelection>;
  restoreList: () => void;
  // Where a card goes: the hero column for the top few, the collapsed tail for the rest.
  place: (card: HTMLElement, rank: number) => void;
  // Expands the collapsed tail if `card` is in it - so selecting a low-rank region from its map
  // marker still scrolls its card into view.
  revealCard: (card: HTMLElement) => void;
}

// Splits the ranked list after the top `heroCount`: the rest collapse behind a "Show N more"
// toggle so the shortlist stays scannable (issue #301). Rebuilds #panel and returns the
// CardContext bits that depend on the split. Exported for its unit test.
export function collapsibleRankList(
  panel: HTMLElement,
  heroCount = 3,
): {
  container: HTMLElement;
  place: CardContext["place"];
  revealCard: CardContext["revealCard"];
  finalize: (total: number) => void;
} {
  panel.innerHTML = `
    <div id="rank-list">
      <button type="button" id="more-regions" class="more-regions" hidden aria-expanded="false"></button>
      <div id="rank-list-more" hidden></div>
    </div>`;
  const container = qs("#rank-list");
  const moreButton = qs<HTMLButtonElement>("#more-regions");
  const moreList = qs("#rank-list-more");
  let open = false;

  const setOpen = (next: boolean, total: number): void => {
    open = next;
    moreList.hidden = !open;
    const hidden = total - heroCount;
    moreButton.textContent = open ? "Show fewer" : `Show ${hidden} more region${hidden === 1 ? "" : "s"}`;
    moreButton.setAttribute("aria-expanded", String(open));
  };

  return {
    container,
    place: (card, rank) => {
      if (rank < heroCount) container.insertBefore(card, moreButton);
      else moreList.appendChild(card);
    },
    revealCard: (card) => {
      if (!open && moreList.contains(card)) {
        open = true;
        moreList.hidden = false;
        moreButton.setAttribute("aria-expanded", "true");
        moreButton.textContent = "Show fewer";
      }
    },
    finalize: (total) => {
      if (total <= heroCount) return;
      moreButton.hidden = false;
      moreButton.onclick = () => setOpen(!open, total);
      setOpen(false, total);
    },
  };
}

// Plots one region's marker, builds its card through the shared builder (ui/card-dom), and
// wires selection / Details / shortlist. Shared by the scored Destinations list and the
// "Active now" (alerts) list - the only differences between the two live in the CardSpec the
// caller passes.
function addCard(
  spec: CardSpec,
  rank: number,
  ctx: CardContext,
): { marker: PlottedMarker; titleNum: HTMLElement } {
  const marker = plot(spec.lat, spec.lng, spec.plotWeight, spec.live, spec.regionId, rank);
  const { card, titleNum } = buildResultCard(spec.model, {
    onSelect: (cardEl) => {
      snapTo("full");
      focusOnMap(spec.lat, spec.lng, 9);
      focusRegion(spec.lat, spec.lng);
      ctx.revealCard(cardEl);
      ctx.cardSelection.select(cardEl, marker);
    },
    onDetails: (cardEl, numEl) => {
      snapTo("full");
      ctx.cardSelection.select(cardEl, marker);
      openDetails({ region_id: spec.regionId }, numEl.textContent ?? spec.regionId, ctx.restoreList);
    },
    onPlan: () => toggleShortlist(spec.regionId),
    isPlanned: () => inShortlist(spec.regionId),
  });
  marker.on("click", (event: LeafletMouseEvent) => {
    // A hero circle's translucent fill covers a lot of map; a click that actually landed on a
    // road/trail line under it should inspect the way, not select the region (the circle
    // swallows the event so the map handler never gets its own shot). See inspectRoadAt.
    if (inspectRoadAt(event.latlng)) return;
    if (sheetEnabled()) {
      snapTo("half"); // a map-pin tap raises the sheet to its middle detent
      focusOnMap(spec.lat, spec.lng, map.getZoom()); // offset clear of the sheet
    }
    focusRegion(spec.lat, spec.lng);
    ctx.revealCard(card);
    ctx.cardSelection.select(card, marker);
  });
  ctx.place(card, rank);
  return { marker, titleNum };
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
      setMonths(state.months); // persist so a reload keeps the selection (not snap to now)
      onScopeChange(); // keep the Months filter pill's label in sync (issue #297)
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

// Last successful /api/destinations payload, kept so a purely presentational change (the km/mi
// toggle - issue #301 F2) can repaint the cards from it instead of re-fetching, which would
// make the toggle depend on the network and briefly clear markers on a transient failure.
let lastRegions: RegionScore[] | null = null;

export async function runDestinations({ reuseCache = false }: { reuseCache?: boolean } = {}): Promise<void> {
  // "Active now" is a different question - "where were my target species just seen?" - answered
  // by /api/alerts, not a re-sort of the scored list. Same view, same card shell (issue #301).
  if (state.sort === "active") return runActiveNow({ reuseCache });

  const isCurrent = destinationsGuard.begin();
  setStatus("Ranking…");
  clearMarkers();
  // No months toggled reads the same as all 12 toggled (monthsParam()'s fallback) - either way
  // there's no actual month restriction, so the phenology chip's %/tooltip need different wording.
  const allMonths = state.months.size === 0 || state.months.size === 12;
  let regions: RegionScore[];
  if (reuseCache && lastRegions) {
    regions = lastRegions;
  } else {
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
    lastRegions = regions;
  }
  if (!isCurrent()) return;
  regions = sortRegions(regions, state.sort); // never mutates lastRegions - sortRegions copies
  const panel = qs("#panel");
  if (!regions.length) {
    panel.innerHTML =
      "<p class='hint'>No regions in range for those months. Try widening months or running Refresh.</p>";
    setStatus("");
    return;
  }
  // The panel is the ranked list of summary cards: top 3 as hero cards, the rest collapsed
  // behind a "Show N more" toggle. Each region's detail tabs open in the Details view
  // (openDetails) from a card's "Details" button - no nested tab strips.
  const list = collapsibleRankList(panel);
  // Only one region's marker shows its true real-world size at a time; selecting a new one
  // reverts whichever marker held that spot back to its score-scaled preview size.
  const cardSelection = createCardSelection(list.container);
  // Card titles start as rank + distance; the notable-place name (issue #206) is filled in
  // afterward through each card's own .num node - see the batch lookup at the end.
  const titleTargets: { region: RegionScore; rank: number; numSpan: HTMLElement }[] = [];

  const renderChips = (region: RegionScore, showAll: boolean): string =>
    region.species
      .slice(0, showAll ? undefined : 6)
      .map((hit) => {
        const pct = Math.round(hit.w_pheno * 100);
        return speciesChip({
          ...hit,
          label: `${pct}% · ${hit.month_count}`,
          title: phenoTitle(pct, hit.month_count, allMonths),
        });
      })
      .join("");

  // Back out of the Details view: re-render the list from the cached payload, no refetch.
  const restoreList = (): void => {
    void runDestinations({ reuseCache: true });
  };
  const ctx: CardContext = {
    cardSelection,
    restoreList,
    place: list.place,
    revealCard: list.revealCard,
  };

  const markers = regions.map((region, rank) => {
    const metaHtml =
      `score <span class="num">${region.score_norm.toFixed(2)}</span> · ` +
      `<span class="num">${region.n_species}</span> spp · ` +
      (region.recent_count ? `<span class="num">${region.recent_count}</span> recent` : "no recent obs") +
      (region.elevation_m != null
        ? ` · elev <span class="num">${elevationLabel(region.elevation_m)}</span>`
        : "") +
      rainMeta(region);

    const model: ResultCardModel = {
      rank,
      titleText: dist(region.distance_km),
      whyHtml: whySentence(region),
      metaHtml,
      scoreNorm: region.score_norm,
      fireHtml: fireBadges(region.fire_nearby),
      renderChips: (showAll) => renderChips(region, showAll),
      chipCount: region.species.length,
      cappedChipCount: 6,
    };

    const { marker, titleNum } = addCard(
      {
        regionId: region.region_id,
        lat: region.center_lat,
        lng: region.center_lng,
        live: region.recent_count > 0,
        plotWeight: region.score_norm,
        model,
      },
      rank,
      ctx,
    );
    titleTargets.push({ region, rank, numSpan: titleNum });
    return marker;
  });
  list.finalize(regions.length);
  setStatus(`${regions.length} regions`);

  // No auto-zoom/pan on results - the map stays wherever the user has it. The (already
  // server-sorted) top result is auto-selected (focus + highlighted card) like a click on #1.
  const top = regions[0];
  const topMarker = markers[0];
  const topCard = list.container.querySelector<HTMLElement>(".rank");
  if (top && topMarker && topCard) {
    focusRegion(top.center_lat, top.center_lng);
    cardSelection.selectInitial(topCard, topMarker);
  }

  // Card titles start as rank + distance only; each card's notable-place name (issue #206)
  // loads afterward. One batch request pulls every title already cached server-side (the
  // common case - a grid cell's centroid never moves, so region_places fills in fast); only
  // the uncached remainder falls back to the per-region endpoint, still one at a time because
  // that path does the throttled Nominatim round-trip (~1/s, see geocode._throttle). So a cold
  // cache is no slower than the old per-card loop and a warm one is a single request (#301 F7).
  // Fire-and-forget: runDestinations() itself doesn't wait on card titles.
  void (async () => {
    const pending = new Map(titleTargets.map((target) => [target.region.region_id, target]));
    const setTitle = (target: (typeof titleTargets)[number], placeName: string): void => {
      target.numSpan.textContent = `#${target.rank + 1} · ${placeName} · ${dist(target.region.distance_km)}`;
    };
    try {
      const places = await getJson("/api/destinations/places", {
        query: { region_ids: [...pending.keys()].join(",") },
      });
      if (!isCurrent()) return;
      for (const [regionId, place] of Object.entries(places)) {
        const target = pending.get(regionId);
        if (!target) continue;
        if (place.place_name) setTitle(target, place.place_name);
        pending.delete(regionId); // cached (a null place_name means "nothing notable" - don't re-query)
      }
    } catch {
      // batch failed - fall through and resolve everything the per-region way
    }
    for (const target of pending.values()) {
      if (!isCurrent()) return;
      let place: RegionPlace;
      try {
        place = await getJson("/api/destinations/{region_id}/place", {
          path: { region_id: target.region.region_id },
        });
      } catch {
        continue; // best-effort - leave this card's title as rank + distance
      }
      if (!isCurrent()) return;
      if (place.place_name) setTitle(target, place.place_name);
    }
  })();
}

// "Active now" branch of the Destinations flow (issue #301): the old "Fruiting now" tab. Lists
// regions where a target genus was seen in the trailing window (/api/alerts), rendered through
// the same card shell as the scored list. No months param (the endpoint uses a fixed
// trailing-weeks window) and no score bar; the card leads with the last-seen line and the
// per-hit observation chips instead.
let lastActive: AlertRegion[] | null = null;

async function runActiveNow({ reuseCache = false }: { reuseCache?: boolean }): Promise<void> {
  // Same guard object as the scored branch - switching the sort mid-fetch has to invalidate
  // whichever branch was in flight, or a stale response overwrites the freshly rendered one
  // (Copilot review).
  const isCurrent = destinationsGuard.begin();
  setStatus("Checking recent activity…");
  clearMarkers();

  let regions: AlertRegion[];
  if (reuseCache && lastActive) {
    regions = lastActive;
  } else {
    try {
      regions = await getJson("/api/alerts");
    } catch (error) {
      if (!isCurrent()) return;
      setStatus(errorDetail(error));
      return;
    }
    lastActive = regions;
  }
  if (!isCurrent()) return;

  const panel = qs("#panel");
  if (!regions.length) {
    panel.innerHTML =
      "<p class='hint'>No target species seen in the trailing window yet. Widen your genera, or switch the sort back to Best overall.</p>";
    setStatus("");
    return;
  }

  const list = collapsibleRankList(panel);
  const cardSelection = createCardSelection(list.container);
  const restoreList = (): void => {
    void runActiveNow({ reuseCache: true });
  };
  const ctx: CardContext = {
    cardSelection,
    restoreList,
    place: list.place,
    revealCard: list.revealCard,
  };

  const renderHits = (region: AlertRegion, showAll: boolean): string =>
    region.species
      .slice(0, showAll ? undefined : 6)
      .map((hit) => {
        const label = `${hit.count} · ${hit.last_seen}${hit.obscured ? " ⚠ fuzzy" : ""}`;
        const safeUri = hit.uri && hit.uri.startsWith("https://") ? hit.uri : null;
        if (safeUri) {
          return `<a class="chip live" href="${escapeHtml(safeUri)}" target="_blank" rel="noopener"
             >${escapeHtml(displayName(hit))} · ${escapeHtml(label)}</a>`;
        }
        return speciesChip({ ...hit, label }, "live");
      })
      .join("");

  const markers = regions.map((region, rank) => {
    const place = region.species[0]?.place_guess ?? null;
    const topHit = region.species[0];
    const whyHtml = topHit
      ? `<strong>${escapeHtml(displayName(topHit))}</strong> last seen ${escapeHtml(topHit.last_seen)}` +
        `${place ? ` near ${escapeHtml(place)}` : ""}.`
      : "";

    const model: ResultCardModel = {
      rank,
      titleText: place ? `${place} · ${dist(region.distance_km)}` : dist(region.distance_km),
      whyHtml,
      metaHtml: `<span class="num">${region.total}</span> recent${rainMeta(region)}`,
      scoreNorm: null,
      fireHtml: fireBadges(region.fire_nearby),
      renderChips: (showAll) => renderHits(region, showAll),
      chipCount: region.species.length,
      cappedChipCount: 6,
    };

    const { marker } = addCard(
      {
        regionId: region.region_id,
        lat: region.center_lat,
        lng: region.center_lng,
        live: true, // every region here is here because it was seen recently
        plotWeight: Math.min(1, region.total / 10),
        model,
      },
      rank,
      ctx,
    );
    return marker;
  });
  list.finalize(regions.length);
  setStatus(`${regions.length} active regions`);

  const top = regions[0];
  const topCard = list.container.querySelector<HTMLElement>(".rank");
  if (top && markers[0] && topCard) {
    focusRegion(top.center_lat, top.center_lng);
    cardSelection.selectInitial(topCard, markers[0]);
  }
}
