// The Google-Maps-style filter-pill row (issue #297). Builds one pill per map filter, moving
// the existing control DOM into each pill's popover (not rebuilding it) so the modules that
// wire those controls - initRadiusPresets, initMonths, initGenusSelection, initLayerToggles -
// keep working unchanged. Pill labels re-render from the same backing state via the
// state.onScopeChange hook, which those handlers already trigger (through updateHome for
// home/radius/units, and explicitly for months / genera / layers).

import { createPill, type Pill } from "./pill";
import { selectedGenera } from "../genera";
import { dist, MONTHS, qs, setScopeChangeHook, state, type View } from "../state";
import { SORT_LABEL } from "../views/sort";

let pills: Pill[] = [];
let sortPill: Pill | null = null;
let monthsPill: Pill | null = null;

// getElementById, not qs(): a pill's render/active callbacks run once inside createPill() while
// the moved-in control node is still detached from the document (the wrap is appended to the
// row afterwards), so the lookup must tolerate a transient miss - the final refreshPills() at
// the end of initPills() recomputes every label with everything in place.
const checkbox = (id: string): HTMLInputElement | null =>
  document.getElementById(id) as HTMLInputElement | null;
const isChecked = (id: string): boolean => checkbox(id)?.checked ?? false;

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

// Sort only applies to the ranked Destinations list. Fruiting now (alerts): no Months (that
// view has no month param - a fixed trailing-weeks window). Layers / Genera stay put on every
// view.
export function syncPillsForView(view: View): void {
  if (sortPill) sortPill.el.hidden = view !== "destinations";
  if (monthsPill) monthsPill.el.hidden = view === "alerts";
}

export function initPills(): void {
  const row = qs("#pills");

  sortPill = createPill({
    label: "Sort",
    render: () => SORT_LABEL[state.sort],
    popover: qs("#sort-options"),
    active: () => state.sort !== "best",
    closeOnSelect: true, // single choice - pick one and it's done
  });

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

  // Every map-overlay toggle now lives behind one "Layers" pill (issue #301: 8 pills -> 3).
  // The label counts how many overlays are on rather than naming them - the popover shows the
  // detail, and the names (BLM / USFS / Campgrounds / Dispersed / ...) don't fit a pill.
  const layerIds = [
    "show-land-blm",
    "show-land-usfs",
    "show-land-tribal",
    "show-camps",
    "show-dispersed",
    "free-camps",
    "show-fire",
    "show-aerial",
  ];
  const layersPill = createPill({
    label: "Layers",
    render: () => {
      const count = layerIds.filter((id) => isChecked(id)).length;
      return count === 0 ? "Off" : `${count} on`;
    },
    popover: qs("#layer-toggles"),
    active: () => layerIds.some((id) => isChecked(id)),
  });

  pills = [sortPill, radiusPill, monthsPill, generaPill, layersPill];
  pills.forEach((pill) => row.appendChild(pill.el));

  // A layer checkbox lives inside a pill popover now, so its own change handler (initLayerToggles)
  // no longer implies a refresh - update the pill label whenever one flips.
  layerIds.forEach((id) => checkbox(id)?.addEventListener("change", refreshPills));

  setScopeChangeHook(refreshPills);
  syncPillsForView(state.view);
  refreshPills();
}
