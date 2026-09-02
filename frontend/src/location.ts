import { getJson } from "./api/client";
import type { PlaceSuggestion } from "./api/types";
import { initAutocomplete } from "./autocomplete";
import { setLocation } from "./refresh";
import { qs } from "./state";

// Place search now goes through the backend (`GET /api/location/search`, issue #145) instead of
// the browser calling Nominatim directly - the server owns the one User-Agent/rate-limit policy,
// and precise coordinates a user types never leave for a third party from the client. The
// autocomplete widget's own `generation` guard drops stale responses, so no client-side abort
// is needed (matches genera.ts).
async function fetchSuggestions(query: string): Promise<PlaceSuggestion[]> {
  try {
    return await getJson("/api/location/search", { query: { q: query } });
  } catch {
    return [];
  }
}

/** Wire a place-search input + suggestion list. ``onSelect`` receives either a resolved
 * "lat, lng" string (suggestion picked) or the raw typed text (form submitted directly),
 * leaving what to do with it (persist as home, stash for a trip-planning field, etc.) to the
 * caller. */
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
  initAutocomplete<PlaceSuggestion>({
    input,
    list,
    form,
    fetchSuggestions,
    label: (result) => result.name,
    onPick: (result) => emit(`${result.lat}, ${result.lng}`),
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
