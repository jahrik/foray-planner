// Shared typeahead widget backing the header place-search (location.ts), the plan-tab
// Start/Destination fields (main.ts), and the genus picker (genera.ts). Each of those wraps the
// same <input> + <ul class="suggestions"> pair with identical behaviour: a debounced fetch, a
// filtered result list, Arrow/Enter/Escape keyboard nav, and a 150ms blur-close (long enough for
// a suggestion's mousedown to land first). All per-widget state lives in the closure below, so
// two instances on one page never collide. No Leaflet, no network - see autocomplete.test.ts.

export interface AutocompleteConfig<T> {
  input: HTMLInputElement;
  list: HTMLUListElement;
  form: HTMLFormElement;
  /** Fetch matches for a query. Return `null` to signal the request was superseded (an aborted
   * fetch, say) so the current list is left untouched; return `[]` for "no matches". */
  fetchSuggestions: (query: string) => Promise<T[] | null>;
  /** Visible text for one suggestion. */
  label: (item: T) => string;
  /** A suggestion was chosen (click or Enter). */
  onPick: (item: T) => void;
  /** Drop items before they're shown (e.g. genera already selected). Applied wherever the
   * result list is read, so keyboard nav indexes the same filtered set. */
  filter?: (item: T) => boolean;
  /** Runs synchronously on every keystroke, before the debounce and min-length checks - a hook
   * for per-keystroke side effects (e.g. cancelling an in-flight fetch). Unused since the
   * place search moved server-side (issue #145); kept for callers that need it. */
  onInput?: () => void;
  /** Form submitted with non-empty text and no suggestion picked: called with the trimmed
   * text. Omit to only preventDefault (the genus picker has no free-text path). */
  onSubmitText?: (text: string) => void;
  /** Characters required before a fetch fires. Default 2. */
  minChars?: number;
  /** Debounce before fetching, in ms. Default 300. */
  debounceMs?: number;
}

export function initAutocomplete<T>(config: AutocompleteConfig<T>): void {
  const { input, list, form, fetchSuggestions, label, onPick } = config;
  const minChars = config.minChars ?? 2;
  const debounceMs = config.debounceMs ?? 300;
  const filter = config.filter ?? (() => true);

  let debounceTimer: ReturnType<typeof setTimeout> | null = null;
  let activeIndex = -1;
  let results: T[] = [];
  // Bumped whenever the list is closed or a new query is dispatched, so a debounced callback or
  // a fetch that resolves after the fact is dropped instead of re-opening a dismissed list.
  let generation = 0;

  function close(): void {
    list.classList.remove("open");
    activeIndex = -1;
    generation += 1;
    if (debounceTimer) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
    }
  }

  function render(): void {
    list.innerHTML = "";
    activeIndex = -1;
    results = results.filter(filter);
    if (!results.length) {
      list.classList.remove("open");
      return;
    }
    results.forEach((item, index) => {
      const li = document.createElement("li");
      li.textContent = label(item);
      li.dataset.index = String(index);
      li.onmousedown = (event) => {
        event.preventDefault();
        close();
        onPick(item);
      };
      list.appendChild(li);
    });
    list.classList.add("open");
  }

  input.addEventListener("input", () => {
    config.onInput?.();
    const query = input.value.trim();
    if (debounceTimer) clearTimeout(debounceTimer);
    if (query.length < minChars) {
      close();
      return;
    }
    const dispatch = (generation += 1);
    debounceTimer = setTimeout(async () => {
      const fetched = await fetchSuggestions(query);
      if (fetched === null || dispatch !== generation) return; // superseded or list closed
      results = fetched;
      render();
    }, debounceMs);
  });

  input.addEventListener("keydown", (event) => {
    const items = list.querySelectorAll<HTMLLIElement>("li");
    if (!items.length || !list.classList.contains("open")) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      activeIndex = Math.min(activeIndex + 1, items.length - 1);
      items.forEach((li, index) => li.classList.toggle("active", index === activeIndex));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      activeIndex = Math.max(activeIndex - 1, 0);
      items.forEach((li, index) => li.classList.toggle("active", index === activeIndex));
    } else if (event.key === "Enter" && activeIndex >= 0) {
      event.preventDefault();
      const item = results[activeIndex];
      if (item) {
        close();
        onPick(item);
      }
    } else if (event.key === "Escape") {
      close();
    }
  });

  input.addEventListener("blur", () => {
    setTimeout(() => list.classList.remove("open"), 150);
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = input.value.trim();
    if (query && config.onSubmitText) {
      close();
      config.onSubmitText(query);
    }
  });
}
