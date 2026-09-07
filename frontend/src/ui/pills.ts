// The Google-Maps-style filter-pill row (issue #297). Builds one pill per map filter, moving
// the existing control DOM into each pill's popover (not rebuilding it) so the modules that
// wire those controls - initRadiusPresets, initMonths, initGenusSelection, initLayerToggles -
// keep working unchanged. Pill labels re-render from the same backing state via the
// state.onScopeChange hook, which those handlers already trigger (through updateHome for
// home/radius/units, and explicitly for months / genera / layers).

import { createPill, type Pill } from "./pill";
import { selectedGenera } from "../genera";
import { dist, MONTHS, qs, setScopeChangeHook, state, type View } from "../state";

let pills: Pill[] = [];
let monthsPill: Pill | null = null;
let campingPill: Pill | null = null;

// getElementById, not qs(): a pill's render/active callbacks run once inside createPill() while
// the moved-in control node is still detached from the document (the wrap is appended to the
// row afterwards), so the lookup must tolerate a transient miss - the final refreshPills() at
// the end of initPills() recomputes every label with everything in place.
const checkbox = (id: string): HTMLInputElement | null =>
  document.getElementById(id) as HTMLInputElement | null;
const isChecked = (id: string): boolean => checkbox(id)?.checked ?? false;

function checkedLabels(entries: [string, string][]): string {
  const on = entries.filter(([id]) => isChecked(id)).map(([, label]) => label);
  return on.length ? on.join(", ") : "Off";
}

function monthsLabel(): string {
  const count = state.months.size;
  if (count === 0 || count === 12) return "All year";
  if (count === 1) return MONTHS[[...state.months][0]! - 1]!;
  return `${count} months`;
}

function generaLabel(): string {
  const genera = selectedGenera();
  if (genera.length === 0) return "All genera";
  const [first, ...rest] = genera;
  return rest.length ? `${first!.name} +${rest.length}` : first!.name;
}

export function refreshPills(): void {
  pills.forEach((pill) => pill.refresh());
}

// Destinations: all pills except Camping. Fruiting now (alerts): no Months (that view has no
// month param - a fixed trailing-weeks window). Plan route: adds Camping.
export function syncPillsForView(view: View): void {
  if (monthsPill) monthsPill.el.hidden = view === "alerts";
  if (campingPill) campingPill.el.hidden = view !== "plan";
}

export function initPills(): void {
  const row = qs("#pills");

  const radiusPill = createPill({
    label: "Radius",
    render: () => dist(state.home?.radius_km ?? 150),
    popover: qs("#radius-presets"),
    closeOnSelect: true, // single choice - pick a preset and it's done
  });

  monthsPill = createPill({
    label: "",
    render: monthsLabel,
    popover: qs("#months"),
    active: () => state.months.size > 0 && state.months.size < 12,
  });

  const generaPill = createPill({
    label: "",
    render: generaLabel,
    popover: qs("#genus-field"),
    active: () => selectedGenera().length > 0,
  });

  const landEntries: [string, string][] = [
    ["show-land-blm", "BLM"],
    ["show-land-usfs", "USFS"],
    ["show-land-tribal", "Tribal"],
  ];
  const landPill = createPill({
    label: "Land",
    render: () => checkedLabels(landEntries),
    popover: qs("#land-toggles"),
    active: () => landEntries.some(([id]) => isChecked(id)),
  });

  const firePill = createPill({
    label: "Fire",
    render: () => (isChecked("show-fire") ? "On" : "Off"),
    active: () => isChecked("show-fire"),
    onToggle: (next) => {
      const input = checkbox("show-fire");
      if (!input) return;
      input.checked = next;
      input.dispatchEvent(new Event("change"));
    },
  });

  const campEntries: [string, string][] = [
    ["show-camps", "Campgrounds"],
    ["show-dispersed", "Dispersed"],
    ["free-camps", "Free only"],
  ];
  campingPill = createPill({
    label: "Camping",
    render: () => checkedLabels(campEntries),
    popover: qs("#camp-toggles"),
    active: () => campEntries.some(([id]) => isChecked(id)),
  });

  pills = [radiusPill, monthsPill, generaPill, landPill, firePill, campingPill];
  pills.forEach((pill) => row.appendChild(pill.el));

  // A layer checkbox lives inside a pill popover now, so its own change handler (initLayerToggles)
  // no longer implies a refresh - update the pill labels whenever one flips.
  ["show-fire", ...landEntries.map(([id]) => id), ...campEntries.map(([id]) => id)].forEach((id) => {
    checkbox(id)?.addEventListener("change", refreshPills);
  });

  setScopeChangeHook(refreshPills);
  syncPillsForView(state.view);
  refreshPills();
}
