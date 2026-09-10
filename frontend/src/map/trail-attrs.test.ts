import { describe, expect, it } from "vitest";

import { gradeLabel, isRoughSurface, seasonalNote } from "./trail-attrs";

describe("isRoughSurface", () => {
  it("is false with no attrs", () => {
    expect(isRoughSurface(null)).toBe(false);
    expect(isRoughSurface(undefined)).toBe(false);
    expect(isRoughSurface({})).toBe(false);
  });

  it("dashes an ungraded track (tracktype grade 4-5)", () => {
    expect(isRoughSurface({ tracktype: "grade4" })).toBe(true);
    expect(isRoughSurface({ tracktype: "grade5" })).toBe(true);
  });

  it("keeps a graded track solid even over a rough surface tag", () => {
    expect(isRoughSurface({ tracktype: "grade1", surface: "dirt" })).toBe(false);
  });

  it("falls back to surface / smoothness when tracktype is absent", () => {
    expect(isRoughSurface({ surface: "ground" })).toBe(true);
    expect(isRoughSurface({ smoothness: "very_bad" })).toBe(true);
    expect(isRoughSurface({ surface: "asphalt" })).toBe(false);
  });
});

describe("gradeLabel", () => {
  it("renders a tracktype as a human grade", () => {
    expect(gradeLabel({ tracktype: "grade3" })).toBe("grade 3");
  });

  it("is null without a valid tracktype", () => {
    expect(gradeLabel({})).toBeNull();
    expect(gradeLabel({ tracktype: "grade9" })).toBeNull();
    expect(gradeLabel(null)).toBeNull();
  });
});

describe("seasonalNote", () => {
  it("is null when the way carries no seasonal / conditional tag", () => {
    expect(seasonalNote(null)).toBeNull();
    expect(seasonalNote({ access: "yes" })).toBeNull();
    expect(seasonalNote({ seasonal: "no" })).toBeNull();
  });

  it("hands back the raw conditional restriction, foot first", () => {
    expect(seasonalNote({ "access:conditional": "no @ (Nov-May)" })).toBe("no @ (Nov-May)");
    expect(
      seasonalNote({ "motor_vehicle:conditional": "no @ (Dec-Mar)", "foot:conditional": "yes @ (Dec-Mar)" }),
    ).toBe("yes @ (Dec-Mar)");
  });

  it("flags a bare seasonal tag", () => {
    expect(seasonalNote({ seasonal: "yes" })).toBe("not open year-round");
    expect(seasonalNote({ seasonal: "winter" })).toBe("winter only");
  });
});
