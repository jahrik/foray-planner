// The Details view (issue #301): the per-region tabs that used to be nested inside every
// destination card - Calendar / Photos / Trails / Campgrounds - moved out here so the result
// list stays a scannable column of summary cards. "Details" on a card swaps #panel to this
// pane; the back button restores the list from its last payload (no refetch). The tab loaders
// themselves are unchanged (views/destination-tabs.ts) - just re-hosted.

import { escapeHtml } from "../format";
import { qs } from "../state";
import { createLazyLoader } from "../ui/lazy-panel";
import { stopLinkPropagation } from "../ui/card-dom";
import {
  loadCalendarInto,
  loadCampgroundsInto,
  loadPhotosInto,
  loadTrailheadsInto,
} from "./destination-tabs";

type DetailTab = "calendar" | "photos" | "trails" | "camps";

// Only the region id is needed - every loader queries by it. Both a scored destination
// (RegionScore) and an "Active now" alert region satisfy this.
export interface DetailRegion {
  region_id: string;
}

export function openDetails(region: DetailRegion, title: string, onBack: () => void): void {
  const panel = qs("#panel");
  panel.innerHTML = `
    <div class="details-view">
      <button type="button" class="details-back">&larr; Back to results</button>
      <h3 class="details-title">${escapeHtml(title)}</h3>
      <div class="rank-tabs" role="tablist">
        <button type="button" class="rank-tab active" data-tab="calendar">Calendar</button>
        <button type="button" class="rank-tab" data-tab="photos">Photos</button>
        <button type="button" class="rank-tab" data-tab="trails">Trails</button>
        <button type="button" class="rank-tab" data-tab="camps">Campgrounds</button>
      </div>
      <div class="rank-calendar" data-tab-content="calendar"></div>
      <div class="rank-photos" data-tab-content="photos" hidden></div>
      <div class="rank-trails" data-tab-content="trails" hidden></div>
      <div class="rank-camps" data-tab-content="camps" hidden></div>
    </div>`;

  qs<HTMLButtonElement>(".details-back", panel).onclick = onBack;

  const bodies: Record<DetailTab, HTMLElement> = {
    calendar: qs('[data-tab-content="calendar"]', panel),
    photos: qs('[data-tab-content="photos"]', panel),
    trails: qs('[data-tab-content="trails"]', panel),
    camps: qs('[data-tab-content="camps"]', panel),
  };
  stopLinkPropagation(bodies.photos);
  stopLinkPropagation(bodies.trails);
  stopLinkPropagation(bodies.camps);

  const loaders: Record<DetailTab, { open: () => void }> = {
    calendar: createLazyLoader(() => loadCalendarInto(region.region_id, bodies.calendar)),
    photos: createLazyLoader(() => loadPhotosInto(region.region_id, bodies.photos)),
    trails: createLazyLoader(() => loadTrailheadsInto(region, bodies.trails)),
    camps: createLazyLoader(() => loadCampgroundsInto(region, bodies.camps)),
  };

  const tabs = [...panel.querySelectorAll<HTMLButtonElement>(".rank-tab")];
  const show = (tab: DetailTab): void => {
    tabs.forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
    (Object.keys(bodies) as DetailTab[]).forEach((key) => {
      bodies[key].hidden = key !== tab;
    });
    loaders[tab].open();
  };
  tabs.forEach((button) => {
    button.onclick = () => show(button.dataset.tab as DetailTab);
  });

  show("calendar");
}
