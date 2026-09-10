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

// The region id plus its observation centroid: the calendar/photos loaders query by id, the
// trails/camps loaders query by the centroid (issue #306 C4 - a grid cell's centre can sit
// offshore). Both a scored destination (RegionScore) and an "Active now" alert region satisfy this.
export interface DetailRegion {
  region_id: string;
  center_lat: number;
  center_lng: number;
}

export function openDetails(region: DetailRegion, title: string, onBack: () => void): void {
  const panel = qs("#panel");
  panel.innerHTML = `
    <div class="details-view">
      <button type="button" class="details-back">&larr; Back to results</button>
      <h3 class="details-title">${escapeHtml(title)}</h3>
      <div class="rank-tabs" role="tablist" aria-label="Region details">
        <button type="button" role="tab" id="dtab-calendar" aria-controls="dpanel-calendar" aria-selected="true" class="rank-tab active" data-tab="calendar">Calendar</button>
        <button type="button" role="tab" id="dtab-photos" aria-controls="dpanel-photos" aria-selected="false" tabindex="-1" class="rank-tab" data-tab="photos">Photos</button>
        <button type="button" role="tab" id="dtab-trails" aria-controls="dpanel-trails" aria-selected="false" tabindex="-1" class="rank-tab" data-tab="trails">Trails</button>
        <button type="button" role="tab" id="dtab-camps" aria-controls="dpanel-camps" aria-selected="false" tabindex="-1" class="rank-tab" data-tab="camps">Campgrounds</button>
      </div>
      <div class="rank-calendar" id="dpanel-calendar" role="tabpanel" aria-labelledby="dtab-calendar" tabindex="0" data-tab-content="calendar"></div>
      <div class="rank-photos" id="dpanel-photos" role="tabpanel" aria-labelledby="dtab-photos" tabindex="0" data-tab-content="photos" hidden></div>
      <div class="rank-trails" id="dpanel-trails" role="tabpanel" aria-labelledby="dtab-trails" tabindex="0" data-tab-content="trails" hidden></div>
      <div class="rank-camps" id="dpanel-camps" role="tabpanel" aria-labelledby="dtab-camps" tabindex="0" data-tab-content="camps" hidden></div>
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
    tabs.forEach((button) => {
      const selected = button.dataset.tab === tab;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1; // roving tabindex - one tab stop for the strip
    });
    (Object.keys(bodies) as DetailTab[]).forEach((key) => {
      bodies[key].hidden = key !== tab;
    });
    loaders[tab].open();
  };
  tabs.forEach((button, index) => {
    button.onclick = () => show(button.dataset.tab as DetailTab);
    // Left/Right move between tabs per the WAI-ARIA tabs pattern.
    button.onkeydown = (event) => {
      const next =
        event.key === "ArrowRight"
          ? tabs[(index + 1) % tabs.length]
          : event.key === "ArrowLeft"
            ? tabs[(index - 1 + tabs.length) % tabs.length]
            : null;
      if (!next) return;
      event.preventDefault();
      next.focus();
      next.click();
    };
  });

  show("calendar");
}
