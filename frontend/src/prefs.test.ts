import { beforeEach, describe, expect, it } from "vitest";

import { getLargeText, getTheme, getUnits, setLargeText, setTheme, setUnits } from "./prefs";

beforeEach(() => localStorage.clear());

describe("prefs", () => {
  it("defaults to dark theme, normal text, miles", () => {
    expect(getTheme()).toBe("dark");
    expect(getLargeText()).toBe(false);
    expect(getUnits()).toBe("mi");
  });

  it("round-trips theme through localStorage", () => {
    setTheme("light");
    expect(getTheme()).toBe("light");
    expect(localStorage.getItem("foray-theme")).toBe("light");
    setTheme("dark");
    expect(getTheme()).toBe("dark");
  });

  it("stores text size as large/normal, not a boolean", () => {
    setLargeText(true);
    expect(localStorage.getItem("foray-text-size")).toBe("large");
    expect(getLargeText()).toBe(true);
    setLargeText(false);
    expect(localStorage.getItem("foray-text-size")).toBe("normal");
    expect(getLargeText()).toBe(false);
  });

  it("only accepts km or mi for units, falling back to mi", () => {
    setUnits("km");
    expect(getUnits()).toBe("km");
    localStorage.setItem("foray-units", "furlongs");
    expect(getUnits()).toBe("mi");
  });
});
