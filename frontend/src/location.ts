import { setLocation } from "./refresh";
import { qs } from "./state";

const NOMINATIM = "https://nominatim.openstreetmap.org/search";
const DEBOUNCE_MS = 300;

let debounceTimer: ReturnType<typeof setTimeout> | null = null;
let activeIndex = -1;
let activeAbort: AbortController | null = null;

interface NominatimResult {
  display_name: string;
  lat: string;
  lon: string;
}

// Aborts any in-flight request before starting a new one, so a slow older response (real risk
// on the flaky RV/Starlink connections this app targets) can't resolve after a newer keystroke
// and overwrite the suggestion list with stale results (issue #99). Returns null (rather than
// []) on abort so the caller can tell "superseded" apart from "no matches" and skip re-rendering.
async function fetchSuggestions(query: string): Promise<NominatimResult[] | null> {
  if (query.length < 2) return [];
  activeAbort?.abort();
  const controller = new AbortController();
  activeAbort = controller;
  const params = new URLSearchParams({ q: query, format: "json", limit: "5" });
  try {
    const resp = await fetch(`${NOMINATIM}?${params}`, { signal: controller.signal });
    if (!resp.ok) return [];
    return await resp.json();
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") return null;
    throw error;
  }
}

function renderSuggestions(results: NominatimResult[], list: HTMLUListElement): void {
  list.innerHTML = "";
  activeIndex = -1;
  if (!results.length) {
    list.classList.remove("open");
    return;
  }
  results.forEach((result, i) => {
    const li = document.createElement("li");
    li.textContent = result.display_name;
    li.dataset.index = String(i);
    li.onmousedown = (e) => {
      e.preventDefault();
      selectResult(result);
    };
    list.appendChild(li);
  });
  list.classList.add("open");
}

function selectResult(result: NominatimResult): void {
  const input = qs<HTMLInputElement>("#loc");
  const list = qs<HTMLUListElement>("#loc-suggestions");
  input.value = "";
  list.classList.remove("open");
  setLocation(`${result.lat}, ${result.lon}`);
}

export function initLocationAutocomplete(): void {
  const input = qs<HTMLInputElement>("#loc");
  const list = qs<HTMLUListElement>("#loc-suggestions");
  const form = qs<HTMLFormElement>("#locform");
  let results: NominatimResult[] = [];

  input.addEventListener("input", () => {
    const query = input.value.trim();
    if (debounceTimer) clearTimeout(debounceTimer);
    if (query.length < 2) {
      list.classList.remove("open");
      return;
    }
    debounceTimer = setTimeout(async () => {
      const fetched = await fetchSuggestions(query);
      if (fetched === null) return; // superseded by a newer request
      results = fetched;
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
      selectResult(results[activeIndex]);
    } else if (e.key === "Escape") {
      list.classList.remove("open");
    }
  });

  input.addEventListener("blur", () => {
    setTimeout(() => list.classList.remove("open"), 150);
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const query = input.value.trim();
    if (query) {
      list.classList.remove("open");
      input.value = "";
      setLocation(query);
    }
  });
}
