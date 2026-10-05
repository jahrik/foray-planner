// Genus icons (issue #449): one small silhouette per shape group (gilled cap, bracket, coral, ...)
// plus bespoke art for the most-observed genera. The backend resolves which key a genus gets
// (src/foray/genus_icons.py) and sends it as `icon` on every genus payload; this module turns that
// key into SVG markup.
//
// The art is `./shapes/<group>.svg` (shape groups: what a fungus looks like in the field, which
// cuts across taxonomic families and orders) and `./genera/<genus>.svg` (bespoke genus art):
// hand-drawn 24x24 silhouettes in one colour (`currentColor`),
// so the surrounding element's `color` paints them - ink on the ochre `--spore` disc on the map,
// text colour beside a genus name in lists. They are stylised category markers, not
// identification aids (AGENTS.md "Not in scope"). Bundled with the client: no runtime fetch.

import type { components } from "../api/schema";

export type GenusIcon = components["schemas"]["GenusResult"]["icon"];

// Every key the API can send, in the order the review gallery shows them (groups first). The
// `Exclude` check below fails `tsc` if the API grows a key this list (and so the art) misses.
export const GENUS_ICON_GROUPS = [
  "gilled",
  "bolete",
  "bracket",
  "crust",
  "coral",
  "puffball",
  "earthstar",
  "cup",
  "morel",
  "tooth",
  "vase",
  "jelly",
  "stinkhorn",
  "leafy-lichen",
  "shrubby-lichen",
  "rust",
  "generic",
] as const satisfies readonly GenusIcon[];

export const GENUS_ICON_BESPOKE = [
  "trametes",
  "amanita",
  "laetiporus",
  "pleurotus",
  "fomitopsis",
  "cerioporus",
  "ganoderma",
  "lactarius",
  "stereum",
  "schizophyllum",
  "mycena",
  "suillus",
  "cladonia",
  "omphalotus",
  "coprinus",
  "flavoparmelia",
  "cantharellus",
  "hericium",
  "desarmillaria",
  "lobaria",
  "hypomyces",
  "chlorophyllum",
  "russula",
  "cortinarius",
  "artomyces",
  "apioperdon",
  "entoloma",
  "agaricus",
  "leucocoprinus",
  "morchella",
] as const satisfies readonly GenusIcon[];

export const GENUS_ICON_KEYS: readonly GenusIcon[] = [...GENUS_ICON_GROUPS, ...GENUS_ICON_BESPOKE];

type Unlisted = Exclude<GenusIcon, (typeof GENUS_ICON_GROUPS)[number] | (typeof GENUS_ICON_BESPOKE)[number]>;
// Compile-time only: `never` unless a key is missing from the lists above.
const everyKeyListed: [Unlisted] extends [never] ? true : Unlisted = true;
void everyKeyListed;

const SVG_FILES = import.meta.glob<string>(["./shapes/*.svg", "./genera/*.svg"], {
  query: "?raw",
  import: "default",
  eager: true,
});

const markupByKey = new Map<string, string>(
  Object.entries(SVG_FILES).map(([path, markup]) => [path.replace(/^.*\/(.+)\.svg$/, "$1"), markup.trim()]),
);

/** The SVG markup for `key`, falling back to the generic icon for a key with no art (a newer
 * backend than this bundle). Static bundled files, so safe to place via innerHTML. */
export function genusIconSvg(key: string | null | undefined): string {
  return markupByKey.get(key ?? "generic") ?? markupByKey.get("generic") ?? "";
}

/** A decorative `<span class="genus-icon">` holding the icon, for beside a genus name. The
 * name itself carries the meaning, so the icon is hidden from assistive tech. */
export function genusIconElement(key: string | null | undefined, className = "genus-icon"): HTMLElement {
  const span = document.createElement("span");
  span.className = className;
  span.setAttribute("aria-hidden", "true");
  span.innerHTML = genusIconSvg(key);
  return span;
}
