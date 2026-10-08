// UI glue for device geolocation: the search bar's pin button, detecting that the user has started
// interacting, and applying or merely offering a fix. The location logic itself is geolocate.ts.

import { type Fix, haversineKm, Locator, saveFix, type Session, TRUSTED_ACCURACY_M } from "./geolocate";
import { loadFire, loadLand } from "./map/layers";
import { updateHome } from "./map/map";
import { getManualHome, setManualHome } from "./prefs";
import { accuracyLabel, dist, qs, setStatus, state } from "./state";
import { refreshCurrentView } from "./views/view-run";

// Wires geolocate.ts's Locator to the page: the search bar's 📍 "Use my location" button, the
// "has the user started interacting?" signal, and what showing / offering a fix does.

const LOCATE_TITLE = "Use my current location";

let locator: Locator | null = null;
let engagedEarly = false;

/** Start noticing user input - call before main()'s first await. The page is visible (and the
 * search box usable) while /api/config loads, so input during startup must count too, or a late
 * fix could still re-centre under someone who's already typing. Any real input means the user is
 * mid-task: from then on a fix that would move them is offered, not applied. Capture phase so a
 * handler that stops propagation can't hide it. */
export function trackEngagement(): void {
  for (const type of ["pointerdown", "keydown", "wheel"] as const) {
    document.addEventListener(
      type,
      (event) => {
        if (!event.isTrusted) return;
        engagedEarly = true;
        locator?.markEngaged();
      },
      { capture: true, passive: true },
    );
  }
}

function showOffer(button: HTMLButtonElement, fix: Fix | null, saved: boolean): void {
  button.classList.toggle("pending", fix !== null);
  const home = state.home;
  if (!fix || !home) {
    button.title = LOCATE_TITLE;
    button.setAttribute("aria-label", LOCATE_TITLE);
    return;
  }
  let label: string;
  let status: string;
  if (saved) {
    // The fix is already home (the user started interacting while it was being saved) - only
    // the map view and the results are behind.
    label = `Location updated to ${home.name} - show results here`;
    status = `Location updated to ${home.name} - tap 📍 to refresh results`;
  } else {
    const moved = dist(haversineKm(home, fix));
    // A coarse fix (IP / cell guess) only ever reaches here against a hand-set home - say how
    // rough it is so "you've moved" doesn't read as fact.
    const rough = fix.accuracyM > TRUSTED_ACCURACY_M ? ` (rough guess, ${accuracyLabel(fix.accuracyM)})` : "";
    label = `Your device puts you ${moved} from ${home.name}${rough} - update location`;
    status = `Your device puts you ${moved} from ${home.name}${rough} - tap 📍 to use it`;
  }
  button.title = label;
  button.setAttribute("aria-label", label);
  setStatus(status);
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
    homeIsManual: () => {
      const manual = getManualHome();
      return (
        manual !== null &&
        state.home !== null &&
        manual.lat === state.home.lat &&
        manual.lng === state.home.lng
      );
    },
    save: saveFix,
    show: (home, fix, recenter) => {
      setManualHome(null); // the device fix is home now
      updateHome(home, { recenter, accuracyM: fix.accuracyM });
      if (!recenter) return;
      refreshCurrentView(); // before loadLand() - see refresh.ts setLocation
      loadLand();
      loadFire();
    },
    offer: (fix, _home, saved = false) => showOffer(button, fix, saved),
    status: setStatus,
  }));
  if (engagedEarly) active.markEngaged();
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
