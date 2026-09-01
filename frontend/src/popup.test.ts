import { describe, expect, it } from "vitest";

import { buildPopup } from "./popup";

describe("buildPopup", () => {
  it("bolds the title and sets caller text via textContent (no markup injection)", () => {
    const root = buildPopup({ title: `<img src=x onerror=alert(1)>`, lines: [`a & b <c>`] });
    const bold = root.querySelector("b");
    expect(bold?.textContent).toBe("<img src=x onerror=alert(1)>");
    expect(root.querySelector("img")).toBeNull();
    expect(root.innerHTML).toContain("&lt;img");
    expect(root.textContent).toBe("<img src=x onerror=alert(1)>a & b <c>");
  });

  it("renders each line on its own row after a <br>", () => {
    const root = buildPopup({ title: "Camp", lines: ["12 mi · free"] });
    expect(root.querySelectorAll("br")).toHaveLength(1);
    expect(root.textContent).toBe("Camp12 mi · free");
  });

  it("appends the title suffix on the title line, before any line break", () => {
    const root = buildPopup({ title: "Stop 2", titleSuffix: " · 40 mi leg", lines: ["Boletus edulis"] });
    expect(root.childNodes[0]?.textContent).toBe("Stop 2");
    expect(root.childNodes[1]?.textContent).toBe(" · 40 mi leg");
    expect((root.childNodes[2] as HTMLElement).tagName).toBe("BR");
  });

  it("adds a new-tab anchor with rel=noopener when a link is given", () => {
    const root = buildPopup({
      title: "Land",
      lines: ["BLM"],
      link: { href: "https://x.test/a", text: "Source ↗" },
    });
    const anchor = root.querySelector("a");
    expect(anchor?.getAttribute("href")).toBe("https://x.test/a");
    expect(anchor?.target).toBe("_blank");
    expect(anchor?.rel).toBe("noopener");
    expect(anchor?.textContent).toBe("Source ↗");
  });

  it("omits the anchor and lines when neither is provided", () => {
    const root = buildPopup({ title: "Destination" });
    expect(root.querySelector("a")).toBeNull();
    expect(root.querySelector("br")).toBeNull();
    expect(root.textContent).toBe("Destination");
  });
});
