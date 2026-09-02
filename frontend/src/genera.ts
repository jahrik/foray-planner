import { deleteJson, getJson, postJson } from "./api/client";
import type { GenusResult } from "./api/types";
import { initAutocomplete } from "./autocomplete";
import { displayName, errorDetail, escapeHtml, qs, setStatus } from "./state";

let selected: GenusResult[] = [];
let onChange: (() => void) | null = null;

async function fetchSuggestions(query: string): Promise<GenusResult[]> {
  try {
    return await getJson("/api/genera", { query: { q: query } });
  } catch {
    return [];
  }
}

function renderChips(): void {
  const container = qs<HTMLDivElement>("#genus-chips");
  container.innerHTML = selected
    .map(
      (genus) => `
      <span class="chip removable" data-taxon-id="${genus.taxon_id}">
        ${escapeHtml(displayName(genus))}
        <button type="button" aria-label="Remove ${escapeHtml(genus.name)}">×</button>
      </span>`,
    )
    .join("");
  container.querySelectorAll<HTMLButtonElement>("button").forEach((button) => {
    const chip = button.closest<HTMLElement>("[data-taxon-id]")!;
    button.onclick = () => removeGenus(Number(chip.dataset.taxonId));
  });
}

async function selectGenus(genus: GenusResult): Promise<void> {
  const input = qs<HTMLInputElement>("#genus");
  const list = qs<HTMLUListElement>("#genus-suggestions");
  input.value = "";
  list.classList.remove("open");
  try {
    await postJson("/api/genera/{taxon_id}", { params: { path: { taxon_id: genus.taxon_id } } });
  } catch (error) {
    setStatus(errorDetail(error) || "couldn't add genus");
    return;
  }
  selected.push(genus);
  selected.sort((left, right) => left.name.localeCompare(right.name));
  renderChips();
  onChange?.();
}

async function removeGenus(taxonId: number): Promise<void> {
  try {
    await deleteJson("/api/genera/{taxon_id}", { params: { path: { taxon_id: taxonId } } });
  } catch (error) {
    setStatus(errorDetail(error) || "couldn't remove genus");
    return;
  }
  selected = selected.filter((genus) => genus.taxon_id !== taxonId);
  renderChips();
  onChange?.();
}

// `onSelectionChange` re-runs the current view (mirrors setLocation's runDestinations() call)
// so a genus add/remove is reflected without the user having to switch tabs to force it.
export async function initGenusSelection(onSelectionChange: () => void): Promise<void> {
  onChange = onSelectionChange;
  try {
    selected = await getJson("/api/genera/selected");
  } catch {
    selected = [];
  }
  renderChips();

  const selectedIds = (): Set<number> => new Set(selected.map((genus) => genus.taxon_id));
  initAutocomplete<GenusResult>({
    input: qs<HTMLInputElement>("#genus"),
    list: qs<HTMLUListElement>("#genus-suggestions"),
    form: qs<HTMLFormElement>("#genusform"),
    fetchSuggestions,
    label: (genus) => displayName(genus),
    filter: (genus) => !selectedIds().has(genus.taxon_id),
    onPick: (genus) => void selectGenus(genus),
  });
}
