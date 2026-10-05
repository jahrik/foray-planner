// Review gallery for the genus icons (issue #449): every icon at the sizes it ships at - the 22 px
// map pin, a cluster badge with its count chip, and the 16 px list chip beside a genus name - on
// the light and dark basemap colours and over aerial imagery. Dev-only (icons.html); open it
// with `npm run dev` to sign off art before it lands on the map.

import "@fontsource-variable/ibm-plex-sans";
import "../tokens.css";
import "./gallery.css";

import { GENUS_ICON_BESPOKE, GENUS_ICON_GROUPS, type GenusIcon, genusIconElement } from "./genus-icons";

// One Esri World Imagery tile of mixed forest (Mount Hood National Forest), the same source as
// the aerial overlay - a realistic busy background for legibility.
const AERIAL_TILE =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/14/5866/2638";

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
  figure.append(large, caption, ...BACKGROUNDS.map((background) => sample(key, background.className)));
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
legend.textContent = `Each card: the art at 72 px, then pin / cluster badge / list chip on ${BACKGROUNDS.map(
  (background) => background.name.toLowerCase(),
).join(", ")}.`;
document.body.append(
  title,
  legend,
  section("Shape groups", GENUS_ICON_GROUPS),
  section("Bespoke genera, most observed first", GENUS_ICON_BESPOKE),
);
