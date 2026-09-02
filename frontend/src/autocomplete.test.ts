import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type AutocompleteConfig, initAutocomplete } from "./autocomplete";

interface Row {
  id: number;
  name: string;
}

const ROWS: Row[] = [
  { id: 1, name: "Alpha" },
  { id: 2, name: "Beta" },
  { id: 3, name: "Gamma" },
];

function setup(overrides: Partial<AutocompleteConfig<Row>> = {}) {
  document.body.innerHTML = `
    <form id="f"><input id="i" /></form>
    <ul id="l"></ul>
  `;
  const input = document.getElementById("i") as HTMLInputElement;
  const list = document.getElementById("l") as HTMLUListElement;
  const form = document.getElementById("f") as HTMLFormElement;
  const onPick = vi.fn();
  const fetchSuggestions = vi.fn(async () => ROWS);
  initAutocomplete<Row>({
    input,
    list,
    form,
    fetchSuggestions,
    label: (row) => row.name,
    onPick,
    ...overrides,
  });
  return { input, list, form, onPick, fetchSuggestions };
}

function type(input: HTMLInputElement, value: string): void {
  input.value = value;
  input.dispatchEvent(new Event("input"));
}

function keydown(input: HTMLInputElement, key: string): void {
  input.dispatchEvent(new KeyboardEvent("keydown", { key, cancelable: true }));
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("initAutocomplete", () => {
  it("debounces, then renders one <li> per suggestion and opens the list", async () => {
    const { input, list, fetchSuggestions } = setup();
    type(input, "al");
    expect(fetchSuggestions).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(300);
    expect(fetchSuggestions).toHaveBeenCalledWith("al");
    expect([...list.querySelectorAll("li")].map((li) => li.textContent)).toEqual(["Alpha", "Beta", "Gamma"]);
    expect(list.classList.contains("open")).toBe(true);
  });

  it("does not fetch below minChars and closes the list", async () => {
    const { input, list, fetchSuggestions } = setup();
    type(input, "al");
    await vi.advanceTimersByTimeAsync(300);
    expect(list.classList.contains("open")).toBe(true);
    type(input, "a");
    await vi.advanceTimersByTimeAsync(300);
    expect(fetchSuggestions).toHaveBeenCalledTimes(1);
    expect(list.classList.contains("open")).toBe(false);
  });

  it("applies filter before rendering and to keyboard nav", async () => {
    const { input, list, onPick } = setup({ filter: (row) => row.id !== 2 });
    type(input, "xx");
    await vi.advanceTimersByTimeAsync(300);
    expect([...list.querySelectorAll("li")].map((li) => li.textContent)).toEqual(["Alpha", "Gamma"]);
    keydown(input, "ArrowDown");
    keydown(input, "ArrowDown");
    keydown(input, "Enter");
    expect(onPick).toHaveBeenCalledWith(ROWS[2]);
  });

  it("ArrowDown/ArrowUp move the .active highlight", async () => {
    const { input, list } = setup();
    type(input, "xx");
    await vi.advanceTimersByTimeAsync(300);
    const items = [...list.querySelectorAll("li")];
    keydown(input, "ArrowDown");
    keydown(input, "ArrowDown");
    expect(items[1]?.classList.contains("active")).toBe(true);
    keydown(input, "ArrowUp");
    expect(items[0]?.classList.contains("active")).toBe(true);
  });

  it("Enter picks the highlighted item and closes the list", async () => {
    const { input, list, onPick } = setup();
    type(input, "xx");
    await vi.advanceTimersByTimeAsync(300);
    keydown(input, "ArrowDown");
    keydown(input, "Enter");
    expect(onPick).toHaveBeenCalledWith(ROWS[0]);
    expect(list.classList.contains("open")).toBe(false);
  });

  it("Escape closes the list without picking", async () => {
    const { input, list, onPick } = setup();
    type(input, "xx");
    await vi.advanceTimersByTimeAsync(300);
    keydown(input, "Escape");
    expect(list.classList.contains("open")).toBe(false);
    expect(onPick).not.toHaveBeenCalled();
  });

  it("mousedown on a suggestion picks it", async () => {
    const { input, list, onPick } = setup();
    type(input, "xx");
    await vi.advanceTimersByTimeAsync(300);
    const second = list.querySelectorAll("li")[1] as HTMLLIElement;
    second.dispatchEvent(new MouseEvent("mousedown", { cancelable: true }));
    expect(onPick).toHaveBeenCalledWith(ROWS[1]);
  });

  it("skips re-render when fetchSuggestions returns null", async () => {
    const { input, list } = setup({ fetchSuggestions: vi.fn(async () => ROWS) });
    type(input, "xx");
    await vi.advanceTimersByTimeAsync(300);
    expect(list.classList.contains("open")).toBe(true);
    const { input: input2, list: list2 } = setup({ fetchSuggestions: vi.fn(async () => null) });
    type(input2, "yy");
    await vi.advanceTimersByTimeAsync(300);
    expect(list2.classList.contains("open")).toBe(false);
    expect(list2.querySelectorAll("li")).toHaveLength(0);
  });

  it("calls onSubmitText with trimmed text on form submit", () => {
    const onSubmitText = vi.fn();
    const { input, form } = setup({ onSubmitText });
    input.value = "  boise  ";
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(onSubmitText).toHaveBeenCalledWith("boise");
  });

  it("runs onInput synchronously on every keystroke", () => {
    const onInput = vi.fn();
    const { input } = setup({ onInput });
    type(input, "a");
    type(input, "ab");
    expect(onInput).toHaveBeenCalledTimes(2);
  });
});
