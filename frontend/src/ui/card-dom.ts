// Shared keyboard/click plumbing for the result cards in the Destinations and Alerts panels
// (views.ts, alerts-view.ts). The cards are plain <div>s for layout flexibility but act as
// buttons, so this makes them keyboard-operable and keeps nested links clickable without also
// activating the card.

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
