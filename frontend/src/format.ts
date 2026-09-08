// Pure formatting / escaping helpers shared across the UI modules. No DOM, no state - keep
// everything here trivially unit-testable (see format.test.ts).

/** Escape text destined for an HTML string template (innerHTML / Leaflet popup strings). */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Minimal XML entity escaping for text that goes into GPX/XML element content. */
export function escapeXml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** Human-readable cost label for a campsite. Prefers the parsed nightly range
 * (`feeLow`/`feeHigh`, issue #306); falls back to a short raw fee string, "fee varies" when
 * the raw fee is a paragraph of prose (RIDB ships those), or "cost unknown". `isFree` is
 * passed separately because some callers carry the free flag on a different object. */
export function feeLabel(
  isFree: boolean,
  fee: string | null | undefined,
  feeLow?: number | null,
  feeHigh?: number | null,
): string {
  if (isFree) return "free";
  if (feeLow != null && feeHigh != null) {
    return feeLow === feeHigh ? `$${feeLow}` : `$${feeLow}–$${feeHigh}`;
  }
  if (fee) return fee.length > 40 ? "fee varies" : fee;
  return "cost unknown";
}
