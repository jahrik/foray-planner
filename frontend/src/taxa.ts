// The Taxa pill: search the taxon catalog (species up to class, by scientific name, common name or
// synonym), add and remove this device's targets, and expose the current selection to the rest of
// the client. A target at any rank drives the full ranking (issue #464).

import { genusIconElement, genusIconHtml } from "./icons/genus-icons";
import { deleteJson, getJson, postJson } from "./api/client";
import type { TaxonResult } from "./api/types";
import { initAutocomplete } from "./ui/autocomplete";
import { displayName, errorDetail, escapeHtml, qs, setStatus } from "./state";

/** The ranks the search can be narrowed to, finest first; `null` searches them all. */
export const SEARCH_RANKS = ["species", "genus", "family", "order", "class"] as const;
export type SearchRank = (typeof SEARCH_RANKS)[number];

let selected: TaxonResult[] = [];
let rankFilter: SearchRank | null = null;
let onChange: (() => void) | null = null;

/** The currently-selected targets (server-side per-device state, loaded by initTaxaSelection).
 * Read-only snapshot for the Taxa filter pill's label (issue #297). */
export function selectedTaxa(): readonly TaxonResult[] {
  return selected;
}

/** A suggestion's text: its name (common name parenthesized), plus the synonym or vernacular name
 * the search matched when that is not the scientific name. */
export function suggestionLabel(taxon: TaxonResult): string {
  const label = displayName(taxon);
  return taxon.matched_name && taxon.matched_name !== taxon.name && taxon.matched_name !== taxon.common_name
    ? `${label} - as "${taxon.matched_name}"`
    : label;
}

async function fetchSuggestions(query: string): Promise<TaxonResult[]> {
  try {
    const params: { q: string; rank?: SearchRank } = { q: query };
    if (rankFilter) params.rank = rankFilter;
    return await getJson("/api/taxa/search", { query: params });
  } catch {
    return [];
  }
}

function rankTag(rank: string): string {
  return `<span class="taxon-rank">${escapeHtml(rank)}</span>`;
}

function renderChips(): void {
  const container = qs<HTMLDivElement>("#taxa-chips");
  container.innerHTML = selected
    .map(
      (taxon) => `
      <span class="chip removable" data-taxon-id="${taxon.taxon_id}">
        ${genusIconHtml(taxon.icon)}${escapeHtml(displayName(taxon))}${rankTag(taxon.rank)}
        <button type="button" aria-label="Remove ${escapeHtml(taxon.name)}">×</button>
      </span>`,
    )
    .join("");
  container.querySelectorAll<HTMLButtonElement>("button").forEach((button) => {
    const chip = button.closest<HTMLElement>("[data-taxon-id]")!;
    button.onclick = () => removeTaxon(Number(chip.dataset.taxonId));
  });
}

function renderRankChips(): void {
  const container = qs<HTMLDivElement>("#taxa-ranks");
  const options: (SearchRank | null)[] = [null, ...SEARCH_RANKS];
  container.innerHTML = options
    .map((rank) => {
      const pressed = rank === rankFilter;
      return `<button type="button" data-rank="${rank ?? ""}" aria-pressed="${pressed}">${rank ?? "any"}</button>`;
    })
    .join("");
  container.querySelectorAll<HTMLButtonElement>("button").forEach((button) => {
    button.onclick = () => {
      rankFilter = (button.dataset.rank || null) as SearchRank | null;
      renderRankChips();
      // Re-run the open search under the new rank.
      qs<HTMLInputElement>("#taxon").dispatchEvent(new Event("input"));
    };
  });
}

async function selectTaxon(taxon: TaxonResult): Promise<void> {
  const input = qs<HTMLInputElement>("#taxon");
  const list = qs<HTMLUListElement>("#taxon-suggestions");
  input.value = "";
  list.classList.remove("open");
  try {
    await postJson("/api/taxa/{taxon_id}", { params: { path: { taxon_id: taxon.taxon_id } } });
  } catch (error) {
    setStatus(errorDetail(error) || "couldn't add that taxon");
    return;
  }
  selected.push(taxon);
  selected.sort((left, right) => left.name.localeCompare(right.name));
  renderChips();
  onChange?.();
}

async function removeTaxon(taxonId: number): Promise<void> {
  try {
    await deleteJson("/api/taxa/{taxon_id}", { params: { path: { taxon_id: taxonId } } });
  } catch (error) {
    setStatus(errorDetail(error) || "couldn't remove that taxon");
    return;
  }
  selected = selected.filter((taxon) => taxon.taxon_id !== taxonId);
  renderChips();
  onChange?.();
}

// `onSelectionChange` re-runs the current view (mirrors setLocation's runDestinations() call)
// so a target add/remove is reflected without the user having to switch tabs to force it.
export async function initTaxaSelection(onSelectionChange: () => void): Promise<void> {
  onChange = onSelectionChange;
  try {
    selected = await getJson("/api/taxa/selected");
  } catch {
    selected = [];
  }
  renderChips();
  renderRankChips();

  const selectedIds = (): Set<number> => new Set(selected.map((taxon) => taxon.taxon_id));
  initAutocomplete<TaxonResult>({
    input: qs<HTMLInputElement>("#taxon"),
    list: qs<HTMLUListElement>("#taxon-suggestions"),
    form: qs<HTMLFormElement>("#taxaform"),
    fetchSuggestions,
    label: suggestionLabel,
    decorate: (taxon) => genusIconElement(taxon.icon),
    filter: (taxon) => !selectedIds().has(taxon.taxon_id),
    onPick: (taxon) => void selectTaxon(taxon),
  });
}
