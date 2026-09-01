import { describe, expect, it } from "vitest";

import { escapeHtml, escapeXml, feeLabel } from "./format";

describe("escapeHtml", () => {
  it("escapes the five HTML-significant characters", () => {
    expect(escapeHtml(`<a href="x" title='y'>&</a>`)).toBe(
      "&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt;",
    );
  });

  it("leaves plain text untouched", () => {
    expect(escapeHtml("Amanita muscaria")).toBe("Amanita muscaria");
  });
});

describe("escapeXml", () => {
  it("escapes &, <, > and double quotes but not single quotes", () => {
    expect(escapeXml(`Tom & Jerry's <trip> "2026"`)).toBe("Tom &amp; Jerry's &lt;trip&gt; &quot;2026&quot;");
  });
});

describe("feeLabel", () => {
  it("returns 'free' when the free flag is set, ignoring any fee text", () => {
    expect(feeLabel(true, "$20")).toBe("free");
  });

  it("returns the raw fee string when not free and a fee is given", () => {
    expect(feeLabel(false, "$20/night")).toBe("$20/night");
  });

  it("falls back to 'cost unknown' when not free and the fee is empty/null/undefined", () => {
    expect(feeLabel(false, null)).toBe("cost unknown");
    expect(feeLabel(false, undefined)).toBe("cost unknown");
    expect(feeLabel(false, "")).toBe("cost unknown");
  });
});
