// Shared Leaflet popup builder. Every popup here is assembled from DOM nodes rather than an
// HTML string so caller-supplied text - names, fees, dates, species from external APIs - is set
// via `textContent` and can never be injected as markup. Hrefs are always server-constructed
// (recreation.gov / OSM / iNaturalist / ArcGIS), so anchors are safe. No state - see popup.test.ts.

export interface PopupLink {
  href: string;
  text: string;
}

export interface PopupSpec {
  /** Bolded first line. */
  title: string;
  /** Plain text appended to the title line, after the bold run (e.g. " · 12 mi leg"). */
  titleSuffix?: string;
  /** Each entry becomes its own <br>-separated text row below the title. */
  lines?: string[];
  /** Optional trailing anchor, opened in a new tab. */
  link?: PopupLink;
}

export function buildPopup(spec: PopupSpec): HTMLElement {
  const root = document.createElement("div");
  const strong = document.createElement("b");
  strong.textContent = spec.title;
  root.append(strong);
  if (spec.titleSuffix) root.append(document.createTextNode(spec.titleSuffix));
  for (const line of spec.lines ?? []) {
    root.append(document.createElement("br"), document.createTextNode(line));
  }
  if (spec.link) {
    const anchor = document.createElement("a");
    anchor.href = spec.link.href;
    anchor.target = "_blank";
    anchor.rel = "noopener";
    anchor.textContent = spec.link.text;
    root.append(document.createElement("br"), anchor);
  }
  return root;
}
