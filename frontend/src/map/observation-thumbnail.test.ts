import { describe, expect, it } from "vitest";

import { thumbnailFigure } from "./observation-thumbnail";

describe("thumbnailFigure", () => {
  it("sets the photo and its attribution as plain text", () => {
    const figure = thumbnailFigure({
      url: "https://inaturalist-open-data.s3.amazonaws.com/photos/1/small.jpg",
      attribution: "(c) <b>someone</b>, CC BY",
      license_code: "cc-by",
    });
    expect(figure.querySelector("img")?.getAttribute("src")).toContain("/small.jpg");
    expect(figure.querySelector("figcaption")?.textContent).toBe("(c) <b>someone</b>, CC BY");
    expect(figure.querySelector("b")).toBeNull();
  });
});
