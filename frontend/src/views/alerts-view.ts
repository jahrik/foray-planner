import { createCardSelection, createRunGuard } from "../ui/card-select";
import { makeActivatable, speciesChip, stopLinkPropagation } from "../ui/card-dom";
import { escapeHtml } from "../format";
import { focusRegion } from "../map/layers";
import { clearMarkers, map, plot } from "../map/map";
import { focusOnMap, sheetEnabled, snapTo } from "../map/sheet";
import type { AlertRegion } from "../api/types";
import { getJson } from "../api/client";
import { dist, displayName, errorDetail, fireBadges, qs, rainMeta, setStatus } from "../state";

// Same overlapping-call guard as runDestinations (views.ts) - runAlerts() can also be triggered
// more than once in flight (tab switches, refreshCurrentView() calls). createRunGuard("alerts")
// also bails if the user has since switched away from the Alerts tab entirely while the fetch
// was in flight, not just if a newer runAlerts() call superseded this one.
const alertsGuard = createRunGuard("alerts");

// See views.ts lastRegions - reused to repaint on a units toggle without a re-fetch (#301 F2).
let lastRegions: AlertRegion[] | null = null;

export async function runAlerts({ reuseCache = false }: { reuseCache?: boolean } = {}): Promise<void> {
  const isCurrent = alertsGuard.begin();
  setStatus("Checking recent activity…");
  clearMarkers();
  let regions: AlertRegion[];
  if (reuseCache && lastRegions) {
    regions = lastRegions;
  } else {
    try {
      regions = await getJson("/api/alerts");
    } catch (error) {
      if (!isCurrent()) return;
      setStatus(errorDetail(error));
      return;
    }
    lastRegions = regions;
  }
  if (!isCurrent()) return;
  const panel = qs("#panel");
  if (!regions.length) {
    panel.innerHTML = "<p class='hint'>No target species observed in the trailing window yet.</p>";
    setStatus("");
    return;
  }
  panel.innerHTML = "<h3 style='margin-top:0'>Fruiting now / recently</h3>";
  const cardSelection = createCardSelection(panel);
  regions.forEach((region, rank) => {
    const weight = Math.min(1, region.total / 10);
    const marker = plot(region.center_lat, region.center_lng, weight, true, region.region_id, rank);
    const card = document.createElement("div");
    card.className = "rank";

    const placeText = region.species[0]?.place_guess ? ` · ${escapeHtml(region.species[0].place_guess)}` : "";
    const rain = rainMeta(region);
    card.innerHTML = `<h3><span><span class="num">${dist(region.distance_km)}</span>${placeText}</span><span class="num">${region.total} recent</span></h3>
      ${rain ? `<div class="meta">${rain.replace(/^ · /, "")}</div>` : ""}
      ${fireBadges(region.fire_nearby)}
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
    const selectCard = () => cardSelection.select(card, marker);
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
