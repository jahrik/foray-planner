import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api/client", () => ({
  getJson: vi.fn(async () => [
    { name: "Bend, Deschutes County, Oregon, United States", lat: 44.05, lng: -121.31 },
  ]),
}));
vi.mock("./refresh", () => ({ setLocation: vi.fn() }));

import { initPlaceAutocomplete } from "./location";

function setup() {
  document.body.innerHTML = `
    <form id="f"><input id="i" /></form>
    <ul id="l"></ul>
  `;
  const input = document.getElementById("i") as HTMLInputElement;
  const onSelect = vi.fn();
  initPlaceAutocomplete(
    input,
    document.getElementById("l") as HTMLUListElement,
    document.getElementById("f") as HTMLFormElement,
    onSelect,
  );
  return { input, onSelect };
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

describe("initPlaceAutocomplete", () => {
  it("passes a picked suggestion's name along with its coordinates", async () => {
    const { input, onSelect } = setup();
    input.value = "bend";
    input.dispatchEvent(new Event("input"));
    await vi.advanceTimersByTimeAsync(300);
    keydown(input, "ArrowDown");
    keydown(input, "Enter");
    expect(onSelect).toHaveBeenCalledWith("44.05, -121.31", "Bend, Deschutes County, Oregon, United States");
  });
});
