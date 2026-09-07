// A filter pill for the Google-Maps-style shell (issue #297): a button showing "Label: value"
// (or just "value") that opens an anchored popover holding an existing control, and closes on
// outside-click, Escape, or another pill opening. Generalizes initHelp's open/close logic and
// the .help-popover absolute positioning.
//
// Presentation only - no new state, no network. The popover keeps whatever control wiring it
// already had; the pill's label re-renders from the same state that wiring mutates, via a
// refresh() the caller hooks into the existing change handlers. A pill with no popover is a
// bare toggle (Fire).

// Dispatched on `document` whenever a pill popover (or the search-bar overflow menu, see
// main.ts) opens, with the opening element as `detail`. Every other pill / menu listens and
// closes itself unless it is the one that opened - so only one floating layer is ever open.
export const OPEN_EVENT = "pill:open";

export interface CreatePillOptions {
  // Static prefix, e.g. "Land". An empty string renders the value alone (Months, Genera).
  label: string;
  // Current value text, recomputed on every refresh().
  render: () => string;
  // Control node moved into the popover. Omit for a bare toggle pill.
  popover?: HTMLElement;
  // Bare-toggle pills only: called with the next state when the pill is clicked.
  onToggle?: (active: boolean) => void;
  // Drives the pill's active (.on) styling: the toggle state for a bare pill, or e.g.
  // "any genus selected" / "any layer on" for a popover pill.
  active?: () => boolean;
  // Close the popover as soon as a control inside it is activated. For single-choice popovers
  // like Radius - Google Maps closes those on pick. Leave off for multi-select popovers
  // (Months, Genera, Land, Camping) so the user can toggle several without it closing.
  closeOnSelect?: boolean;
}

export interface Pill {
  // The wrapper (button plus popover). Append this to the pill row.
  readonly el: HTMLElement;
  readonly button: HTMLButtonElement;
  // Re-render the label and active state from current backing state.
  refresh: () => void;
  open: () => void;
  close: () => void;
  isOpen: () => boolean;
}

export function createPill(options: CreatePillOptions): Pill {
  const wrap = document.createElement("div");
  wrap.className = "pill-wrap";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "pill";
  wrap.appendChild(button);

  const popover = options.popover ?? null;
  if (popover) {
    popover.classList.add("pill-popover");
    popover.hidden = true;
    wrap.appendChild(popover);
    button.setAttribute("aria-haspopup", "true");
    button.setAttribute("aria-expanded", "false");
  }

  const labelText = (): string => {
    const value = options.render();
    return options.label ? `${options.label}: ${value}` : value;
  };

  const refresh = (): void => {
    button.textContent = labelText();
    const on = options.active?.() ?? false;
    button.classList.toggle("on", on);
    if (options.onToggle && !popover) button.setAttribute("aria-pressed", String(on));
  };

  const setOpen = (next: boolean): void => {
    if (!popover) return;
    popover.hidden = !next;
    button.setAttribute("aria-expanded", String(next));
    wrap.classList.toggle("open", next);
    if (next) {
      // Flip to right-anchored when a left-anchored popover would spill past the viewport
      // (a pill near the right edge of a narrow screen). jsdom returns a zero rect, so tests
      // are unaffected.
      popover.style.left = "";
      popover.style.right = "";
      if (popover.getBoundingClientRect().right > document.documentElement.clientWidth - 8) {
        popover.style.left = "auto";
        popover.style.right = "0";
      }
      document.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail: wrap }));
    }
  };

  button.addEventListener("click", (event) => {
    event.stopPropagation();
    if (popover) {
      setOpen(popover.hidden);
    } else if (options.onToggle) {
      options.onToggle(!(options.active?.() ?? false));
      refresh();
    }
  });

  if (popover) {
    popover.addEventListener("click", (event) => {
      event.stopPropagation();
      // Single-choice popovers close on pick (Radius, Months). Only a real control activation
      // counts, not a stray click on the popover's own padding.
      if (options.closeOnSelect && (event.target as HTMLElement).closest("button, input, a")) {
        setOpen(false);
      }
    });
    document.addEventListener("click", () => setOpen(false));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") setOpen(false);
    });
    document.addEventListener(OPEN_EVENT, (event) => {
      if ((event as CustomEvent).detail !== wrap) setOpen(false);
    });
  }

  refresh();

  return {
    el: wrap,
    button,
    refresh,
    open: () => setOpen(true),
    close: () => setOpen(false),
    isOpen: () => Boolean(popover) && !popover!.hidden,
  };
}
