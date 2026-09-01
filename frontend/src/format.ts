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

/** Human-readable cost label for a campsite: "free", the raw fee string, or "cost unknown"
 * when the source gave us neither. `isFree` is passed separately because some callers carry
 * the free flag on a different object than the fee text. */
export function feeLabel(isFree: boolean, fee: string | null | undefined): string {
  if (isFree) return "free";
  return fee ? fee : "cost unknown";
}
