// Review gallery for the genus icons (issue #449): every icon at the sizes it ships at - the 22 px
// map pin, a cluster badge with its count chip, and the 16 px list chip beside a genus name - on
// the light and dark basemap colours and over aerial imagery. Dev-only (icons.html); open it
// with `npm run dev` to sign off art before it lands on the map.
//
// Each card also shows a reference photo: the genus's (or, for a shape group, an example genus's)
// iNaturalist default photo, fetched live at view time with its attribution, so review is "does
// the icon read like this?" rather than from memory. Nothing is copied or committed: the art is
// drawn by hand and the photos are only looked at here (see the Art section of #449).

import "@fontsource-variable/ibm-plex-sans";
import "../tokens.css";
import "./gallery.css";

import { GENUS_ICON_BESPOKE, GENUS_ICON_GROUPS, type GenusIcon, genusIconElement } from "./genus-icons";

// One Esri World Imagery tile of mixed forest (Mount Hood National Forest), the same source as
// the aerial overlay - a realistic busy background for legibility.
const AERIAL_TILE =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/14/5866/2638";

// An example genus per shape group, for its reference photo.
const GROUP_EXAMPLES: Partial<Record<GenusIcon, string>> = {
  gilled: "Gymnopilus",
  bolete: "Boletus",
  bracket: "Phellinus",
  crust: "Peniophora",
  coral: "Ramaria",
  puffball: "Lycoperdon",
  earthstar: "Geastrum",
  cup: "Peziza",
  morel: "Gyromitra",
  tooth: "Hydnum",
  vase: "Craterellus",
  jelly: "Tremella",
  stinkhorn: "Phallus",
  "leafy-lichen": "Parmelia",
  "shrubby-lichen": "Usnea",
  rust: "Gymnosporangium",
};

interface InatTaxaPage {
  results?: { name?: string; default_photo?: { square_url?: string; attribution?: string } | null }[];
}

const references: { genus: string; slot: HTMLElement }[] = [];

// One request at a time, politely: 46 lookups once per page load.
async function loadReferences(): Promise<void> {
  for (const { genus, slot } of references) {
    try {
      const url = `https://api.inaturalist.org/v1/taxa?rank=genus&per_page=1&q=${encodeURIComponent(genus)}`;
      const page = (await (await fetch(url)).json()) as InatTaxaPage;
      const hit = page.results?.find((result) => result.name === genus);
      const photo = hit?.default_photo;
      if (!photo?.square_url) {
        slot.textContent = `${genus}: no iNat photo`;
        continue;
      }
      const image = document.createElement("img");
      image.src = photo.square_url;
      image.alt = `${genus} reference photo`;
      image.loading = "lazy";
      const credit = document.createElement("small");
      credit.textContent = `${genus}. ${photo.attribution ?? ""}`;
      slot.replaceChildren(image, credit);
    } catch {
      slot.textContent = `${genus}: photo unavailable`;
    }
  }
}

const BACKGROUNDS = [
  { name: "Light map", className: "bg-light" },
  { name: "Dark map", className: "bg-dark" },
  { name: "Aerial", className: "bg-aerial" },
] as const;

function sample(key: GenusIcon, backgroundClass: string): HTMLElement {
  const row = document.createElement("div");
  row.className = `sample ${backgroundClass}`;
  const pin = genusIconElement(key, "gallery-pin");
  const badge = genusIconElement(key, "gallery-badge");
  const count = document.createElement("b");
  count.textContent = "24";
  badge.append(count);
  const chip = document.createElement("span");
  chip.className = "gallery-chip";
  chip.append(genusIconElement(key), key);
  row.append(pin, badge, chip);
  return row;
}

function card(key: GenusIcon): HTMLElement {
  const figure = document.createElement("figure");
  figure.className = "icon-card";
  const large = genusIconElement(key, "gallery-large");
  const caption = document.createElement("figcaption");
  caption.textContent = key;
  const head = document.createElement("div");
  head.className = "card-head";
  head.append(large);
  const genus = (GENUS_ICON_BESPOKE as readonly string[]).includes(key)
    ? key.charAt(0).toUpperCase() + key.slice(1)
    : GROUP_EXAMPLES[key];
  if (genus) {
    const slot = document.createElement("div");
    slot.className = "reference";
    slot.textContent = "loading reference...";
    head.append(slot);
    references.push({ genus, slot });
  }
  figure.append(head, caption, ...BACKGROUNDS.map((background) => sample(key, background.className)));
  return figure;
}

function section(title: string, keys: readonly GenusIcon[]): HTMLElement {
  const wrapper = document.createElement("section");
  const heading = document.createElement("h2");
  heading.textContent = `${title} (${keys.length})`;
  const grid = document.createElement("div");
  grid.className = "icon-grid";
  grid.append(...keys.map(card));
  wrapper.append(heading, grid);
  return wrapper;
}

document.documentElement.style.setProperty("--aerial-tile", `url("${AERIAL_TILE}")`);
const title = document.createElement("h1");
title.textContent = "Genus icons";
const legend = document.createElement("p");
legend.textContent = `Each card: the art at 72 px beside an iNaturalist reference photo, then pin / cluster badge / list chip on ${BACKGROUNDS.map(
  (background) => background.name.toLowerCase(),
).join(", ")}.`;
document.body.append(
  title,
  legend,
  section("Shape groups", GENUS_ICON_GROUPS),
  section("Bespoke genera, most observed first", GENUS_ICON_BESPOKE),
);
void loadReferences();
