import { initAutocomplete } from "./autocomplete";
import { withLoading } from "./loading";
import { setLocation } from "./refresh";
import { qs } from "./state";

const NOMINATIM = "https://nominatim.openstreetmap.org/search";

let activeAbort: AbortController | null = null;

interface NominatimResult {
  display_name: string;
  lat: string;
  lon: string;
}

// Returns null (rather than []) on abort so the widget can tell "superseded" apart from "no
// matches" and skip re-rendering.
async function fetchSuggestions(query: string): Promise<NominatimResult[] | null> {
  const controller = new AbortController();
  activeAbort = controller;
  const params = new URLSearchParams({ q: query, format: "json", limit: "5" });
  try {
    const resp = await withLoading(() => fetch(`${NOMINATIM}?${params}`, { signal: controller.signal }));
    if (!resp.ok) return [];
    return await resp.json();
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") return null;
    throw error;
  }
}

/** Wire a Nominatim-backed place-search input + suggestion list. ``onSelect`` receives either a
 * resolved "lat, lng" string (suggestion picked) or the raw typed text (form submitted
 * directly), leaving what to do with it (persist as home, stash for a trip-planning field, etc.)
 * to the caller. */
export function initPlaceAutocomplete(
  input: HTMLInputElement,
  list: HTMLUListElement,
  form: HTMLFormElement,
  onSelect: (query: string) => void,
  options: { clearInputOnSelect?: boolean } = {},
): void {
  const clearInputOnSelect = options.clearInputOnSelect ?? true;
  const emit = (query: string): void => {
    if (clearInputOnSelect) input.value = "";
    onSelect(query);
  };
  initAutocomplete<NominatimResult>({
    input,
    list,
    form,
    fetchSuggestions,
    label: (result) => result.display_name,
    // Abort immediately on every keystroke, not just when a new fetch starts - otherwise a
    // request already in flight when the debounce clock is still running (the user deletes
    // back below 2 chars, or types again before the previous fetch resolves) survives and can
    // still resolve later, re-opening the list with stale results (issue #99 follow-up).
    onInput: () => activeAbort?.abort(),
    onPick: (result) => emit(`${result.lat}, ${result.lon}`),
    onSubmitText: emit,
  });
}

export function initLocationAutocomplete(): void {
  initPlaceAutocomplete(
    qs<HTMLInputElement>("#loc"),
    qs<HTMLUListElement>("#loc-suggestions"),
    qs<HTMLFormElement>("#locform"),
    setLocation,
  );
}
