import { describe, expect, it } from "vitest";

import type { PreciseObservation } from "../api/types";
import { countByGenus, voteGenus } from "./cluster-vote";

let nextId = 1;
function obs(
  taxonId: number,
  name: string,
  icon: PreciseObservation["icon"] = "generic",
): PreciseObservation {
  return {
    id: nextId++,
    taxon_id: taxonId,
    name,
    common_name: null,
    icon,
    lat: 45,
    lng: -121,
    observed_on: null,
    uri: null,
  };
}

describe("voteGenus", () => {
  it("picks the most common genus in the cluster", () => {
    const pins = [
      obs(1, "Trametes", "trametes"),
      obs(2, "Amanita", "amanita"),
      obs(1, "Trametes", "trametes"),
    ];
    expect(voteGenus(pins, new Map())).toEqual({ taxonId: 1, name: "Trametes", icon: "trametes", count: 2 });
  });

  it("breaks a tie by the genus more observed overall", () => {
    const pins = [obs(1, "Amanita", "amanita"), obs(2, "Suillus", "suillus")];
    expect(
      voteGenus(
        pins,
        new Map([
          [2, 40],
          [1, 3],
        ]),
      )?.name,
    ).toBe("Suillus");
  });

  it("then by name, so equal genera never flicker", () => {
    const pins = [obs(2, "Suillus"), obs(1, "Amanita")];
    expect(voteGenus(pins, new Map())?.name).toBe("Amanita");
    expect(voteGenus([...pins].reverse(), new Map())?.name).toBe("Amanita");
  });

  it("returns null for an empty cluster", () => {
    expect(voteGenus([], new Map())).toBeNull();
  });

  it("handles a 2,000-pin cluster quickly", () => {
    const pins = Array.from({ length: 2000 }, (_, index) => obs(index % 37, `Genus${index % 37}`));
    const started = performance.now();
    voteGenus(pins, countByGenus(pins));
    expect(performance.now() - started).toBeLessThan(50);
  });
});

describe("countByGenus", () => {
  it("counts pins per taxon", () => {
    expect([...countByGenus([obs(1, "A"), obs(1, "A"), obs(2, "B")])]).toEqual([
      [1, 2],
      [2, 1],
    ]);
  });
});
