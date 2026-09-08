import { runPlan } from "./plan";
import { state } from "../state";
import { runDestinations } from "./views";

// The single "re-run whatever panel is currently open" entry point (issue #242 Part 2e). Used
// after anything that changes the underlying data or scoping inputs - a finished refresh, a
// genus add/remove, a home/radius change - so the new result shows up without the user having
// to switch views to force it. (runDestinations itself dispatches to the "Active now" branch
// when the sort is set to active - issue #301.)
export function refreshCurrentView(): void {
  if (state.view === "plan") runPlan();
  else runDestinations();
}

// Repaint the open panel from its last payload without hitting the network - for a purely
// presentational change like the km/mi toggle (issue #301 F2). Falls back to a normal (fetching)
// run when there's no cached payload yet.
export function rerenderCurrentView(): void {
  if (state.view === "plan") void runPlan({ reuseCache: true });
  else void runDestinations({ reuseCache: true });
}
