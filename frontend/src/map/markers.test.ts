import { describe, expect, it } from "vitest";

import { circleStyle } from "./markers";

describe("circleStyle", () => {
  it("always disables event bubbling and defaults the stroke to the fill colour", () => {
    expect(circleStyle({ radius: 6, fill: "#abc" })).toEqual({
      radius: 6,
      color: "#abc",
      fillColor: "#abc",
      bubblingMouseEvents: false,
    });
  });

  it("keeps a distinct stroke colour when given", () => {
    const style = circleStyle({ radius: 7, fill: "#fff", stroke: "#000" });
    expect(style.color).toBe("#000");
    expect(style.fillColor).toBe("#fff");
  });

  it("passes through the optional path options that are set", () => {
    expect(
      circleStyle({ radius: 5, fill: "#f0f", weight: 2, fillOpacity: 0.35, opacity: 0.8, dashArray: "3 3" }),
    ).toEqual({
      radius: 5,
      color: "#f0f",
      fillColor: "#f0f",
      bubblingMouseEvents: false,
      weight: 2,
      fillOpacity: 0.35,
      opacity: 0.8,
      dashArray: "3 3",
    });
  });

  it("omits optional keys that are undefined rather than setting them", () => {
    const style = circleStyle({ radius: 5, fill: "#f0f", dashArray: undefined });
    expect("dashArray" in style).toBe(false);
    expect("weight" in style).toBe(false);
    expect("opacity" in style).toBe(false);
  });
});
