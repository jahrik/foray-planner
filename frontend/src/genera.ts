import { getJson } from "./api/client";
import type { GenusResult } from "./api/types";
import { displayName, errorDetail, escapeHtml, qs, setStatus } from "./state";

const DEBOUNCE_MS = 300;

let debounceTimer: ReturnType<typeof setTimeout> | null = null;
let activeIndex = -1;
let selected: GenusResult[] = [];
let onChange: (() => void) | null = null;

async function fetchSuggestions(query: string): Promise<GenusResult[]> {
  try {
    return await getJson("/api/genera", { query: { q: query } });
  } catch {
    return [];
  }
}

function renderSuggestions(results: GenusResult[], list: HTMLUListElement): void {
  list.innerHTML = "";
  activeIndex = -1;
  const selectedIds = new Set(selected.map((genus) => genus.taxon_id));
  const candidates = results.filter((genus) => !selectedIds.has(genus.taxon_id));
  if (!candidates.length) {
    list.classList.remove("open");
    return;
  }
  candidates.forEach((genus, i) => {
    const li = document.createElement("li");
    li.textContent = displayName(genus);
    li.dataset.index = String(i);
    li.onmousedown = (e) => {
      e.preventDefault();
      selectGenus(genus);
    };
    list.appendChild(li);
  });
  list.classList.add("open");
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
  let resp: Response;
  try {
    resp = await fetch(`/api/genera/${genus.taxon_id}`, { method: "POST" });
  } catch (error) {
    setStatus(errorDetail(error) || "couldn't add genus");
    return;
  }
  if (!resp.ok) {
    setStatus("couldn't add genus");
    return;
  }
  selected.push(genus);
  selected.sort((left, right) => left.name.localeCompare(right.name));
  renderChips();
  onChange?.();
}

async function removeGenus(taxonId: number): Promise<void> {
  let resp: Response;
  try {
    resp = await fetch(`/api/genera/${taxonId}`, { method: "DELETE" });
  } catch (error) {
    setStatus(errorDetail(error) || "couldn't remove genus");
    return;
  }
  if (!resp.ok) {
    setStatus("couldn't remove genus");
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

  const input = qs<HTMLInputElement>("#genus");
  const list = qs<HTMLUListElement>("#genus-suggestions");
  const form = qs<HTMLFormElement>("#genusform");
  let results: GenusResult[] = [];

  input.addEventListener("input", () => {
    const query = input.value.trim();
    if (debounceTimer) clearTimeout(debounceTimer);
    if (query.length < 2) {
      list.classList.remove("open");
      return;
    }
    debounceTimer = setTimeout(async () => {
      results = await fetchSuggestions(query);
      renderSuggestions(results, list);
    }, DEBOUNCE_MS);
  });

  input.addEventListener("keydown", (e) => {
    const items = list.querySelectorAll("li");
    if (!items.length || !list.classList.contains("open")) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIndex = Math.min(activeIndex + 1, items.length - 1);
      items.forEach((li, i) => li.classList.toggle("active", i === activeIndex));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = Math.max(activeIndex - 1, 0);
      items.forEach((li, i) => li.classList.toggle("active", i === activeIndex));
    } else if (e.key === "Enter" && activeIndex >= 0) {
      e.preventDefault();
      const selectedIds = new Set(selected.map((genus) => genus.taxon_id));
      const candidates = results.filter((genus) => !selectedIds.has(genus.taxon_id));
      selectGenus(candidates[activeIndex]);
    } else if (e.key === "Escape") {
      list.classList.remove("open");
    }
  });

  input.addEventListener("blur", () => {
    setTimeout(() => list.classList.remove("open"), 150);
  });

  form.addEventListener("submit", (e) => e.preventDefault());
}
