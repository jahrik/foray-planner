import { type Fix, haversineKm, Locator, saveFix, type Session } from "./geolocate";
import { loadFire, loadLand } from "./map/layers";
import { updateHome } from "./map/map";
import { dist, qs, setStatus, state } from "./state";
import { refreshCurrentView } from "./views/view-run";

// Wires geolocate.ts's Locator to the page: the search bar's 📍 "Use my location" button, the
// "has the user started interacting?" signal, and what showing / offering a fix does.

const LOCATE_TITLE = "Use my current location";

let locator: Locator | null = null;

function showOffer(button: HTMLButtonElement, fix: Fix | null): void {
  button.classList.toggle("pending", fix !== null);
  const home = state.home;
  if (!fix || !home) {
    button.title = LOCATE_TITLE;
    button.setAttribute("aria-label", LOCATE_TITLE);
    return;
  }
  const moved = dist(haversineKm(home, fix));
  const label = `You're ${moved} from ${home.name} - update location`;
  button.title = label;
  button.setAttribute("aria-label", label);
  setStatus(`You're now ${moved} from ${home.name} - tap 📍 to update your location`);
}

/** Start auto-detecting on load. Returns the session (for main()'s first-paint race), or null
 * when the browser has no Geolocation API - the button is hidden then too. */
export function initAutoLocate(): Session | null {
  const button = qs<HTMLButtonElement>("#locate-me");
  if (!("geolocation" in navigator)) {
    button.hidden = true;
    return null;
  }
  const active = (locator = new Locator({
    geolocation: navigator.geolocation,
    currentHome: () => state.home,
    save: saveFix,
    show: (home, fix, recenter) => {
      updateHome(home, { recenter, accuracyM: fix.accuracyM });
      if (!recenter) return;
      refreshCurrentView(); // before loadLand() - see refresh.ts setLocation
      loadLand();
      loadFire();
    },
    offer: (fix) => showOffer(button, fix),
    status: setStatus,
  }));
  // Any real input means the user is mid-task: from here on a fix that would move them is
  // offered, not applied. Capture phase so a handler that stops propagation can't hide it.
  for (const type of ["pointerdown", "keydown", "wheel"] as const) {
    document.addEventListener(
      type,
      (event) => {
        if (event.isTrusted) active.markEngaged();
      },
      { capture: true, passive: true },
    );
  }
  button.onclick = () => {
    if (active.acceptPending()) return;
    setStatus("Finding your location…");
    active.start({ explicit: true });
  };
  return active.start();
}

/** A location was set by hand - stop any GPS session so a late fix can't overwrite it. */
export function cancelAutoLocate(): void {
  locator?.cancel();
}
