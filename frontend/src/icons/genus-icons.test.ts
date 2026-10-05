import { describe, expect, it } from "vitest";

import {
  GENUS_ICON_BESPOKE,
  GENUS_ICON_GROUPS,
  GENUS_ICON_KEYS,
  genusIconElement,
  genusIconSvg,
} from "./genus-icons";

const files = import.meta.glob<string>(["./shapes/*.svg", "./genera/*.svg"], {
  query: "?raw",
  import: "default",
  eager: true,
});
const fileKeys = Object.keys(files).map((path) => path.replace(/^.*\/(.+)\.svg$/, "$1"));

describe("genus icon art", () => {
  it("has exactly one SVG per API icon key", () => {
    expect([...fileKeys].sort()).toEqual([...GENUS_ICON_KEYS].sort());
  });

  it("keeps shape groups in shapes/ and genus art in genera/", () => {
    const folderOf = (key: string) => Object.keys(files).find((path) => path.endsWith(`/${key}.svg`));
    expect(GENUS_ICON_GROUPS.map(folderOf).every((path) => path?.startsWith("./shapes/"))).toBe(true);
    expect(GENUS_ICON_BESPOKE.map(folderOf).every((path) => path?.startsWith("./genera/"))).toBe(true);
  });

  it.each(Object.entries(files))("%s is a plain 24x24 one-colour silhouette", (_path, markup) => {
    const doc = new DOMParser().parseFromString(markup, "image/svg+xml");
    const root = doc.documentElement;
    expect(root.tagName).toBe("svg");
    expect(root.getAttribute("viewBox")).toBe("0 0 24 24");
    // Only shapes: no scripts, styles, ids (which would collide across many inline copies),
    // links, or embedded images.
    const tags = new Set([...root.querySelectorAll("*")].map((node) => node.tagName));
    expect([...tags].every((tag) => tag === "path")).toBe(true);
    expect(markup).not.toMatch(/\bid=|href|<script|<style|on\w+=/i);
    // One colour: every fill/stroke is currentColor (or none).
    for (const color of markup.matchAll(/(?:fill|stroke)="([^"]+)"/g)) {
      expect(["currentColor", "none", "evenodd"]).toContain(color[1]);
    }
    expect(markup.length).toBeLessThan(1300);
  });
});

describe("genusIconSvg", () => {
  it("returns the art for a known key", () => {
    expect(genusIconSvg("morchella")).toBe(files["./genera/morchella.svg"]?.trim());
  });

  it("falls back to the generic icon for a missing or unknown key", () => {
    const generic = files["./shapes/generic.svg"]?.trim();
    expect(genusIconSvg(undefined)).toBe(generic);
    expect(genusIconSvg(null)).toBe(generic);
    expect(genusIconSvg("not-a-key")).toBe(generic);
  });
});

describe("genusIconElement", () => {
  it("is a decorative span holding the svg", () => {
    const element = genusIconElement("trametes");
    expect(element.className).toBe("genus-icon");
    expect(element.getAttribute("aria-hidden")).toBe("true");
    expect(element.querySelector("svg")).not.toBeNull();
  });
});
