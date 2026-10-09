import { describe, expect, it } from "vitest";
import type { TaxonResult } from "./api/types";
import { SEARCH_RANKS, suggestionLabel } from "./taxa";

const taxon = (overrides: Partial<TaxonResult>): TaxonResult => ({
  taxon_id: 1,
  name: "Cantharellus formosus",
  common_name: null,
  rank: "species",
  icon: "generic",
  matched_name: null,
  ...overrides,
});

describe("suggestionLabel", () => {
  it("is the scientific name when there is no common name", () => {
    expect(suggestionLabel(taxon({}))).toBe("Cantharellus formosus");
  });

  it("parenthesizes the common name", () => {
    expect(suggestionLabel(taxon({ common_name: "Pacific golden chanterelle" }))).toBe(
      "Cantharellus formosus (Pacific golden chanterelle)",
    );
  });

  it("says which synonym matched when it is neither name", () => {
    expect(suggestionLabel(taxon({ matched_name: "Cantharellus cibarius var. formosus" }))).toBe(
      'Cantharellus formosus - as "Cantharellus cibarius var. formosus"',
    );
  });

  it("does not repeat the common name as a match", () => {
    const hit = taxon({ common_name: "Golden chanterelle", matched_name: "Golden chanterelle" });
    expect(suggestionLabel(hit)).toBe("Cantharellus formosus (Golden chanterelle)");
  });
});

describe("SEARCH_RANKS", () => {
  it("runs finest to coarsest, species through class", () => {
    expect([...SEARCH_RANKS]).toEqual(["species", "genus", "family", "order", "class"]);
  });
});
