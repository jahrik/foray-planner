// "Use as trip stop" (issue #311): pin the specific trailhead / trail / forest road / campground
// a user picked in a destination's Details view as that stop's exact point, instead of the
// region centroid. One contextual button sits under each Trails / Campgrounds list - hidden
// until a row is selected, then offering to pin (or unpin) that row - so the chip rows stay
// unchanged and it works the same on touch. The pinned row's chip is marked via `data-pin-key`,
// re-synced across both tabs of the open Details view on every change.

import { setStatus } from "../state";
import { pinFor, setPin, type PinRef } from "./shortlist";

function pinKey(ref: Pick<PinRef, "kind" | "id">): string {
  return `${ref.kind}:${ref.id}`;
}

/** Tag a list chip as the row for `ref`, so syncPinnedChips can mark it when pinned. */
export function markPinnable(chip: HTMLElement, ref: PinRef): void {
  chip.dataset.pinKey = pinKey(ref);
}

/** Mark the chip of `regionId`'s current pin (and unmark every other) under `root`. */
export function syncPinnedChips(root: ParentNode, regionId: string): void {
  const pinned = pinFor(regionId);
  const key = pinned ? pinKey(pinned) : null;
  root.querySelectorAll<HTMLElement>("[data-pin-key]").forEach((chip) => {
    chip.classList.toggle("pinned", chip.dataset.pinKey === key);
  });
}

export interface PinAction {
  element: HTMLButtonElement;
  /** Point the button at a newly selected row. */
  select: (ref: PinRef) => void;
}

/** The "Use as trip stop" button for one Details list. `regionId` is the destination it pins. */
export function createPinAction(regionId: string): PinAction {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "card-action pin-action";
  button.hidden = true;
  let selected: PinRef | null = null;

  const render = (): void => {
    if (!selected) return;
    const pinned = pinFor(regionId);
    const on = pinned !== undefined && pinKey(pinned) === pinKey(selected);
    button.textContent = on ? `✓ Trip stop: ${selected.name}` : `📍 Use ${selected.name} as trip stop`;
    button.title = on ? "Tap to unpin - the stop goes back to the destination centre" : "";
    button.classList.toggle("on", on);
    button.setAttribute("aria-pressed", String(on));
  };

  button.onclick = (event) => {
    event.stopPropagation();
    if (!selected) return;
    const pinned = pinFor(regionId);
    const on = pinned !== undefined && pinKey(pinned) === pinKey(selected);
    setPin(regionId, on ? null : selected);
    setStatus(on ? "Trip stop unpinned" : `Trip stop set: ${selected.name} - Plan a route to use it`);
    render();
    syncPinnedChips(button.closest(".details-view") ?? document, regionId);
  };

  return {
    element: button,
    select: (ref) => {
      selected = ref;
      button.hidden = false;
      render();
    },
  };
}
