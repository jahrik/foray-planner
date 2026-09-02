import { runAlerts } from "./alerts-view";
import { runPlan } from "./plan";
import { state } from "./state";
import { runDestinations } from "./views";

// The single "re-run whatever panel is currently open" entry point (issue #242 Part 2e). Used
// after anything that changes the underlying data or scoping inputs - a finished refresh, a
// genus add/remove, a home/radius change - so the new result shows up without the user having
// to switch tabs to force it. Previously duplicated as an inline `state.view` switch in main.ts,
// refresh.ts, and initTabs.
export function refreshCurrentView(): void {
  if (state.view === "destinations") runDestinations();
  else if (state.view === "alerts") runAlerts();
  else if (state.view === "plan") runPlan();
}
