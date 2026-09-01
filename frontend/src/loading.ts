// Global indeterminate loading bar (issue #228). A shared in-flight counter drives one thin
// top-of-viewport bar: the first outstanding request shows it, the last one to settle hides it
// (after a short fade so a burst of quick calls doesn't flicker). This is purely visual - the
// detailed message + live-region announcement stays on `#status` (setStatus in state.ts), and
// the determinate Refresh bar stays on `#refresh-progress`.

let inFlight = 0;
let hideTimer: ReturnType<typeof setTimeout> | null = null;

function bar(): HTMLElement | null {
  return document.getElementById("load-bar");
}

function show(): void {
  if (hideTimer !== null) {
    clearTimeout(hideTimer);
    hideTimer = null;
  }
  const element = bar();
  if (!element) return;
  element.classList.add("active");
  element.setAttribute("aria-hidden", "false");
}

function hide(): void {
  const element = bar();
  if (!element) return;
  element.classList.remove("active");
  hideTimer = setTimeout(() => {
    hideTimer = null;
    element.setAttribute("aria-hidden", "true");
  }, 250);
}

export function beginLoading(): void {
  inFlight += 1;
  if (inFlight === 1) show();
}

export function endLoading(): void {
  inFlight = Math.max(0, inFlight - 1);
  if (inFlight === 0) hide();
}

/** Run an async task with the global bar held up for its duration. Always decrements, even if
 * `task` rejects. */
export async function withLoading<T>(task: () => Promise<T>): Promise<T> {
  beginLoading();
  try {
    return await task();
  } finally {
    endLoading();
  }
}
