import { afterEach, describe, expect, it, vi } from "vitest";

import { createPill, type Pill } from "./pill";

afterEach(() => {
  document.body.innerHTML = "";
});

function mount(pill: Pill): void {
  document.body.appendChild(pill.el);
}

describe("createPill", () => {
  it("renders 'Label: value' and re-renders on refresh from backing state", () => {
    let value = "50 km";
    const pill = createPill({
      label: "Radius",
      render: () => value,
      popover: document.createElement("div"),
    });
    mount(pill);
    expect(pill.button.textContent).toBe("Radius: 50 km");
    value = "300 km";
    pill.refresh();
    expect(pill.button.textContent).toBe("Radius: 300 km");
  });

  it("renders the value alone when the label is empty", () => {
    const pill = createPill({
      label: "",
      render: () => "Sep",
      popover: document.createElement("div"),
    });
    mount(pill);
    expect(pill.button.textContent).toBe("Sep");
  });

  it("toggles the popover open and closed on click", () => {
    const popover = document.createElement("div");
    const pill = createPill({ label: "L", render: () => "v", popover });
    mount(pill);
    expect(popover.hidden).toBe(true);
    pill.button.click();
    expect(popover.hidden).toBe(false);
    expect(pill.button.getAttribute("aria-expanded")).toBe("true");
    expect(pill.isOpen()).toBe(true);
    pill.button.click();
    expect(popover.hidden).toBe(true);
    expect(pill.isOpen()).toBe(false);
  });

  it("closes on an outside click but not on a click inside the popover", () => {
    const popover = document.createElement("div");
    const inner = document.createElement("button");
    popover.appendChild(inner);
    const pill = createPill({ label: "L", render: () => "v", popover });
    mount(pill);
    pill.button.click();
    inner.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(popover.hidden).toBe(false);
    document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(popover.hidden).toBe(true);
  });

  it("closes on Escape", () => {
    const popover = document.createElement("div");
    const pill = createPill({ label: "L", render: () => "v", popover });
    mount(pill);
    pill.button.click();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(popover.hidden).toBe(true);
  });

  it("opening one pill closes any other open pill", () => {
    const popoverA = document.createElement("div");
    const popoverB = document.createElement("div");
    const pillA = createPill({ label: "A", render: () => "a", popover: popoverA });
    const pillB = createPill({ label: "B", render: () => "b", popover: popoverB });
    mount(pillA);
    mount(pillB);
    pillA.button.click();
    expect(popoverA.hidden).toBe(false);
    pillB.button.click();
    expect(popoverA.hidden).toBe(true);
    expect(popoverB.hidden).toBe(false);
  });

  it("a bare toggle pill flips its active state and calls onToggle", () => {
    let on = false;
    const onToggle = vi.fn((next: boolean) => {
      on = next;
    });
    const pill = createPill({
      label: "Fire",
      render: () => (on ? "On" : "Off"),
      onToggle,
      active: () => on,
    });
    mount(pill);
    expect(pill.button.classList.contains("on")).toBe(false);
    pill.button.click();
    expect(onToggle).toHaveBeenCalledWith(true);
    expect(pill.button.classList.contains("on")).toBe(true);
    expect(pill.button.getAttribute("aria-pressed")).toBe("true");
    expect(pill.button.textContent).toBe("Fire: On");
  });
});
