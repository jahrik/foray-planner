// Shared result-card DOM + keyboard/click plumbing for the Destinations list and the "Active
// now" list (both in views.ts). The cards are plain <div>s for layout flexibility but act as
// buttons, so this makes them keyboard-operable and keeps nested links clickable without also
// activating the card. buildResultCard() is the one card template both views render through
// (issue #301) - a scannable summary card; the per-region detail tabs live in the Details view
// (views/details.ts) now, not nested inside every card.

import { escapeHtml } from "../format";
import { displayName, inatUrl } from "../state";

// Cards act as buttons (selecting a region) but are plain <div>s for layout flexibility, so make
// them keyboard-operable: focusable, and Enter/Space activates - but only when the key event's
// target is the card itself, not a nested button/link (those already get native keyboard
// activation, and re-triggering the card on top of that would double-fire).
export function makeActivatable(card: HTMLElement, activate: () => void): void {
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
export function stopLinkPropagation(container: HTMLElement): void {
  container.addEventListener("click", (event) => {
    if (event.target instanceof Element && event.target.closest("a")) event.stopPropagation();
  });
}

// name/common_name/label ultimately come from iNaturalist (user-editable), so callers escape
// before interpolating into an HTML string template.
export interface ChipData {
  taxon_id: number;
  name: string;
  common_name?: string | null;
  label?: string;
  title?: string;
}

// name/common_name/label ultimately come from iNaturalist (user-editable), so escape before
// interpolating into an HTML string template.
export const speciesChip = (hit: ChipData, extraClass?: string): string =>
  `<a class="chip${extraClass ? " " + extraClass : ""}" href="${inatUrl(hit.taxon_id)}"
      target="_blank" rel="noopener"${hit.title ? ` title="${escapeHtml(hit.title)}"` : ""}
   >${escapeHtml(displayName(hit))}${hit.label ? " · " + escapeHtml(hit.label) : ""}</a>`;

// The one result-card shape, rendered by both the Destinations rank list and the "Active now"
// now (alerts) list. A scannable summary only - rank + title, the plain-language line, a meta row,
// species/hit chips with show-more, fire badges, and a two-button action row. The per-region
// detail tabs (Calendar / Photos / Trails / Campgrounds) are not here any more; "Details" opens
// them in the dedicated Details view (views/details.ts).
export interface ResultCardModel {
  rank: number;
  // Right side of the title line - the distance at first, swapped for "Place - distance" once
  // the notable-place lookup resolves (views.ts writes through the returned titleNum node).
  titleText: string;
  // Pre-rendered HTML: the "why" sentence for a destination, the last-seen line for an alert.
  // Empty string omits the line entirely.
  whyHtml: string;
  // Pre-rendered HTML meta row (score / spp / recent / elev / rain, or "N recent - rain").
  metaHtml: string;
  // 0..1 for the score bar; null omits the bar (alert regions have no score).
  scoreNorm: number | null;
  // Pre-rendered fire-badge HTML (state.fireBadges) - empty string when nothing nearby.
  fireHtml: string;
  // Renders the chip row; called with false for the capped view and true on "show all".
  renderChips: (showAll: boolean) => string;
  chipCount: number;
  cappedChipCount: number;
}

export interface ResultCardHandlers {
  // Passed the card element so the caller can hand it to its card-selection manager without a
  // forward reference back into its own map() closure.
  onSelect: (card: HTMLElement) => void;
  onDetails: (card: HTMLElement, titleNum: HTMLElement) => void;
  onPlan: () => void;
  isPlanned: () => boolean;
}

export function buildResultCard(
  model: ResultCardModel,
  handlers: ResultCardHandlers,
): { card: HTMLElement; titleNum: HTMLElement } {
  const card = document.createElement("div");
  card.className = model.rank < 3 ? "rank hero" : "rank";
  const hasMore = model.chipCount > model.cappedChipCount;
  card.innerHTML = `
    <h3><span class="num">#${model.rank + 1} · ${escapeHtml(model.titleText)}</span></h3>
    ${model.whyHtml ? `<p class="why">${model.whyHtml}</p>` : ""}
    ${model.scoreNorm != null ? `<div class="bar"><span style="width:${(model.scoreNorm * 100).toFixed(0)}%"></span></div>` : ""}
    <div class="meta">${model.metaHtml}</div>
    ${model.fireHtml}
    <div class="chips">${model.renderChips(false)}</div>
    ${hasMore ? `<button type="button" class="show-more" aria-expanded="false">Show all ${model.chipCount}</button>` : ""}
    <div class="card-actions">
      <button type="button" class="card-action" data-act="details">Details</button>
      <button type="button" class="card-action card-action-plan" data-act="plan"></button>
    </div>`;

  const titleNum = card.querySelector<HTMLElement>(".num")!;
  const chips = card.querySelector<HTMLElement>(".chips")!;
  stopLinkPropagation(chips);

  const showMore = card.querySelector<HTMLButtonElement>(".show-more");
  if (showMore) {
    let expanded = false;
    showMore.onclick = (event) => {
      event.stopPropagation();
      expanded = !expanded;
      chips.innerHTML = model.renderChips(expanded);
      showMore.textContent = expanded ? "Show less" : `Show all ${model.chipCount}`;
      showMore.setAttribute("aria-expanded", String(expanded));
    };
  }

  const planButton = card.querySelector<HTMLButtonElement>('[data-act="plan"]')!;
  const syncPlan = (): void => {
    const on = handlers.isPlanned();
    planButton.textContent = on ? "✓ In route" : "+ Plan";
    planButton.classList.toggle("on", on);
    planButton.setAttribute("aria-pressed", String(on));
  };
  syncPlan();
  planButton.onclick = (event) => {
    event.stopPropagation();
    handlers.onPlan();
    syncPlan();
  };

  card.querySelector<HTMLButtonElement>('[data-act="details"]')!.onclick = (event) => {
    event.stopPropagation();
    handlers.onDetails(card, titleNum);
  };

  makeActivatable(card, () => handlers.onSelect(card));
  return { card, titleNum };
}
