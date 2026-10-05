# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0"]
# ///
"""Record the how-to GIFs + step screenshots in docs/tutorials/ against the live site.

Each tutorial drives a real browser (Playwright) through one task. A caption bar and a
visible cursor are drawn into the page itself, so the recording needs no post-production:
frames are captured from the first caption on and stitched into a GIF with ffmpeg, and
every captioned step also saves a screenshot for the written guide (README.md).

Run with `just tutorials` (or `just tutorials region-details` for one). Needs `ffmpeg`.
Recording reads the live site; choosing a location / genera saves them for the recording
browser's own fresh anonymous device id only.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# playwright comes from the inline script metadata above, not the project venv `just lint` checks.
from playwright.sync_api import (  # ty: ignore[unresolved-import]
    CDPSession,
    Locator,
    Page,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # ty: ignore[unresolved-import]

OUT_DIR = Path(__file__).resolve().parent
IMG_DIR = OUT_DIR / "img"
# `--instagram` output (gitignored - regenerated, and meant for posting, not the repo).
INSTAGRAM_DIR = OUT_DIR / "instagram"
DEFAULT_URL = "https://forayplanner.com/"
INSTAGRAM_DEFAULT = ["best-spot", "track-down", "mobile"]
# The home every tutorial searches for, as the origin of any Google Maps page a tutorial opens
# (Tutorial.follow_link). A find's Directions link carries no origin - it routes from wherever the
# user is - so Maps would start at "Your location" from the recording machine's IP and publish it
# in the GIF. The trip route's origin is the home's coordinates, which Maps labels with whatever
# business sits on that point; the place name reads as what it is.
MAPS_ORIGIN = "Bend, Oregon"


@dataclass(frozen=True)
class Profile:
    """Viewport in CSS px, device scale factor, and whether to emulate a phone."""

    viewport: dict[str, int]
    scale: float
    mobile: bool


DESKTOP = Profile({"width": 1280, "height": 760}, 1, mobile=False)
# 2x so the phone walkthrough stays sharp when shown larger.
MOBILE = Profile({"width": 390, "height": 760}, 2, mobile=True)
# Instagram Reels are 1080x1920 (9:16). The desktop layout needs > 780 CSS px, so 810x1440 at
# 4/3; the phone layout at 405x720 x 8/3. Both land on exactly 1080x1920 device pixels.
INSTAGRAM_DESKTOP = Profile({"width": 810, "height": 1440}, 1080 / 810, mobile=False)
INSTAGRAM_MOBILE = Profile({"width": 405, "height": 720}, 1080 / 405, mobile=True)
# Feed carousel stills are 1080x1350 (4:5): the same widths, shorter.
CAROUSEL_VIEWPORTS = {False: {"width": 810, "height": 1012}, True: {"width": 405, "height": 506}}
# (instagram, phone walkthrough) -> profile
PROFILES = {
    (False, False): DESKTOP,
    (False, True): MOBILE,
    (True, False): INSTAGRAM_DESKTOP,
    (True, True): INSTAGRAM_MOBILE,
}

# Injected once per page: a caption bar pinned to the bottom and a cursor dot that follows
# the (synthetic) mouse - headless Chromium draws no pointer of its own.
OVERLAY_JS = """
() => {
  if (document.getElementById('tut-caption')) return;
  const style = document.createElement('style');
  style.textContent = `
    #tut-caption { position: fixed; left: 16px; right: 16px; bottom: 34px; margin: 0 auto;
      width: fit-content; z-index: 2147483647; max-width: 1200px; padding: 14px 28px; border-radius: 14px;
      background: rgba(20, 16, 12, .92); color: #f6efe6; font: 700 42px/1.2 system-ui, sans-serif;
      box-shadow: 0 6px 24px rgba(0,0,0,.45); text-align: center; pointer-events: none;
      transition: opacity .2s; }
    #tut-caption[data-empty] { opacity: 0; }
    /* Sized for the GIF being viewed shrunk to phone width: a 1280px desktop frame shows at
       ~360px on a phone, so desktop captions need to be ~3.5x what reads at full size. */
    @media (max-width: 500px) { #tut-caption { font-size: 21px; bottom: 74px; padding: 10px 14px; } }
    #tut-caption b { color: #f0a46a; }
    #tut-cursor { position: fixed; z-index: 2147483647; width: 22px; height: 22px; margin: -11px 0 0 -11px;
      border-radius: 50%; background: rgba(255, 196, 120, .55); border: 2px solid #fff;
      box-shadow: 0 0 0 2px rgba(0,0,0,.35); pointer-events: none; transition: transform .12s; }
    #tut-cursor.down { transform: scale(.7); background: rgba(255, 140, 60, .85); }
  `;
  document.head.appendChild(style);
  const caption = document.createElement('div');
  caption.id = 'tut-caption';
  caption.dataset.empty = '';
  document.body.appendChild(caption);
  const cursor = document.createElement('div');
  cursor.id = 'tut-cursor';
  cursor.style.left = '-40px';
  document.body.appendChild(cursor);
  addEventListener('mousemove', (event) => {
    cursor.style.left = event.clientX + 'px';
    cursor.style.top = event.clientY + 'px';
  }, true);
  addEventListener('mousedown', () => cursor.classList.add('down'), true);
  addEventListener('mouseup', () => cursor.classList.remove('down'), true);
}
"""


# True when a page is a bot-check interstitial instead of the content (see Tutorial.follow_link).
BOT_CHECK_JS = """
() => /just a moment|attention required|security verification|verify you are human/i.test(
  document.title + ' ' + (document.body?.innerText || '').slice(0, 600))
"""


@dataclass
class Tutorial:
    page: Page
    slug: str
    img_dir: Path = IMG_DIR
    on_start: Callable[[], None] | None = None
    # Instagram mode: also save each step at this (4:5) viewport into carousel_dir, pausing the
    # recording (`cast`) around the resize so it never shows in the Reel.
    carousel_viewport: dict[str, int] | None = None
    carousel_dir: Path | None = None
    cast: Screencast | None = None
    # Phone walkthrough: taps just land on their target. A gliding pointer would also read as a
    # mouse leaving the open popup, and the hover controller would close it.
    touch: bool = False
    started: bool = False
    steps: list[tuple[str, str]] = field(default_factory=list)

    def caption(self, html: str, *, shot: str | None = None, hold: float = 2.2) -> None:
        """Show a caption, optionally save it as a numbered guide screenshot, then hold."""
        self.page.evaluate(OVERLAY_JS)
        self.page.evaluate(
            "(html) => { const el = document.getElementById('tut-caption');"
            " el.innerHTML = html; delete el.dataset.empty; }",
            html,
        )
        if not self.started:
            self.started = True
            if self.on_start:
                self.on_start()
        time.sleep(0.4)
        if shot:
            name = f"{self.slug}-{len(self.steps) + 1:02d}-{shot}.png"
            # The cursor dot helps the GIF but would cover text in a still.
            self.page.screenshot(path=self.img_dir / name, type="png", style="#tut-cursor { display: none; }")
            self.steps.append((name, html))
        time.sleep(hold)
        # After the hold, not before: the resize freezes the Reel's frame and the map re-fits when
        # it's restored, so doing it mid-caption read as a stall followed by a jump. Here it lands
        # between the caption and the next move.
        if shot:
            self._carousel_still(f"{self.slug}-{len(self.steps):02d}-{shot}.png")

    def _carousel_still(self, name: str) -> None:
        viewport, carousel_dir = self.carousel_viewport, self.carousel_dir
        if viewport is None or carousel_dir is None:
            return
        if self.cast:
            self.cast.paused = True
        original = self.page.viewport_size
        self.page.set_viewport_size({"width": viewport["width"], "height": viewport["height"]})
        time.sleep(0.8)  # Leaflet re-fits the map on resize
        self.page.screenshot(path=carousel_dir / name, type="png", style="#tut-cursor { display: none; }")
        if original:
            self.page.set_viewport_size(original)
        time.sleep(0.8)
        if self.cast:
            self.cast.paused = False

    def point(self, target: Locator) -> None:
        """Glide the visible cursor onto `target` (tap targets on mobile just jump)."""
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        if box is None or self.touch:
            return
        glide(self.page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=18)
        time.sleep(0.25)

    def click(self, target: Locator, *, pause: float = 0.6) -> None:
        self.point(target)
        target.click()
        time.sleep(pause)

    def follow_link(self, link: Locator, html: str, *, hold: float = 3.4) -> None:
        """Click a link that opens a new tab, record that tab under a caption, then close it.

        The screencast follows the tab, so the GIF shows the page the link really opens
        (iNaturalist, Google Maps). GIF only - no numbered guide screenshot of a third-party page.
        """
        original = self.page
        self.point(link)
        link.evaluate(PIN_ORIGIN_JS, MAPS_ORIGIN)
        with original.context.expect_page(timeout=15_000) as opened:
            link.click()
        external = opened.value
        # Trackers can hold "load" open; what has painted by then is enough to show.
        with contextlib.suppress(PlaywrightTimeoutError):
            external.wait_for_load_state("load", timeout=30_000)
        # A multi-stop Maps route keeps loading well past "load"; wait for it to go quiet.
        with contextlib.suppress(PlaywrightTimeoutError):
            external.wait_for_load_state("networkidle", timeout=10_000)
        time.sleep(1.5)  # let tiles / photos paint
        # Some sites answer a headless browser with a bot check (Cloudflare's "Just a moment...")
        # instead of the page. Leave that visit out of the recording rather than show the check.
        blocked = external.evaluate(BOT_CHECK_JS)
        if blocked:
            print(f"{self.slug}: {external.url} served a bot check - leaving that visit out")
            external.close()
            original.bring_to_front()
            return
        if self.cast:
            self.cast.switch_to(external)
        self.page = external
        try:
            self.caption(html, hold=hold)
        finally:
            self.page = original
            if self.cast:
                self.cast.switch_to(original)
            external.close()
            original.bring_to_front()
            time.sleep(0.6)

    def type_slowly(self, target: Locator, text: str) -> None:
        self.click(target)
        target.press_sequentially(text, delay=90)
        time.sleep(0.8)


# Last pointer position per page, so glide() can interpolate from where the pointer actually is.
POINTER: dict[int, tuple[float, float]] = {}


def glide(page: Page, x: float, y: float, *, steps: int = 18, delay: float = 0.016) -> None:
    """Move the pointer to (x, y) in eased hops with a pause between each.

    A bare `mouse.move(..., steps=n)` fires every hop back to back, so the screencast, which only
    catches repaints, sees a handful of frames and a cursor or drag looks like it jumps.
    """
    start_x, start_y = POINTER.get(id(page), (x, y))
    for step in range(1, steps + 1):
        progress = step / steps
        eased = progress * progress * (3 - 2 * progress)
        page.mouse.move(start_x + (x - start_x) * eased, start_y + (y - start_y) * eased)
        time.sleep(delay)
    POINTER[id(page)] = (x, y)


def wait_for_home(page: Page, name: str) -> None:
    """Block until the header names the new home. The ranked list re-renders asynchronously
    after a location change, so the old location's cards still match `.rank` meanwhile."""
    page.wait_for_function(
        "(name) => (document.getElementById('home-name')?.textContent || '').includes(name)",
        arg=name,
        timeout=60_000,
    )


def wait_for_results(page: Page) -> None:
    page.locator("#panel .rank").first.wait_for(timeout=60_000)
    time.sleep(2.5)  # let the map settle + markers paint


def set_home(tut: Tutorial, query: str) -> None:
    page = tut.page
    tut.type_slowly(page.locator("#loc"), query)
    suggestion = page.locator("#loc-suggestions li").first
    suggestion.wait_for(timeout=15_000)
    tut.click(suggestion, pause=1.0)
    wait_for_home(page, query.split(",")[0])
    wait_for_results(page)


# --- tutorials ---------------------------------------------------------------------------


def getting_started(tut: Tutorial) -> None:
    page = tut.page
    tut.caption(
        "Foray Planner ranks where target fungi are being found <b>right now</b>, near you.",
        shot="overview",
        hold=3,
    )
    tut.caption("Start by searching for <b>where you are</b> (or where you're headed).")
    set_home(tut, "Bend, Oregon")
    tut.caption("The list re-ranks around your new home, best destination first.", shot="home-set")

    months = page.locator("#pills .pill-wrap").nth(2).locator(".pill")
    tut.click(months)
    tut.caption("<b>Months</b>: pick when you'll be out. It defaults to this month.", shot="months")
    page.keyboard.press("Escape")
    time.sleep(0.5)

    genera = page.locator("#pills .pill-wrap").nth(3).locator(".pill")
    tut.click(genera)
    tut.caption("<b>Genera</b>: narrow the ranking to the mushrooms you're after.")
    tut.type_slowly(page.locator("#genus"), "Cantharellus")
    suggestion = page.locator("#genus-suggestions li").first
    suggestion.wait_for(timeout=15_000)
    tut.click(suggestion, pause=1.0)
    tut.caption("Chanterelles added. Add as many genera as you like.", shot="genera")
    page.keyboard.press("Escape")
    wait_for_results(page)

    card = page.locator("#panel .rank").first
    tut.point(card.locator(".why"))
    tut.caption(
        "Each card leads with <b>why</b> it ranks: what's in season and how many records, "
        "plus rain and access when known.",
        shot="card",
        hold=3.2,
    )
    tut.click(card.locator("h3"), pause=1.5)
    tut.caption("Click a card to fly the map to that destination.", shot="selected", hold=2.6)

    radius = page.locator("#pills .pill-wrap").nth(1).locator(".pill")
    tut.click(radius)
    tut.caption("<b>Radius</b> sets how far from home to search.", shot="radius")
    page.keyboard.press("Escape")
    sort = page.locator("#pills .pill-wrap").nth(0).locator(".pill")
    tut.click(sort)
    tut.caption("<b>Sort</b> by best overall, what's active now, or nearest.", shot="sort")
    page.keyboard.press("Escape")
    tut.caption("That's the basics. Next: open a destination's <b>Details</b>.", hold=2.5)


# Screen position of one single (unclustered) precise-observation pin inside the map viewport,
# or None (map.ts precisePinIcon).
FIND_PIN_JS = """
() => {
  const map = document.getElementById('map').getBoundingClientRect();
  const panel = document.getElementById('dock')?.getBoundingClientRect();
  for (const pin of document.querySelectorAll('#map .precise-pin-icon')) {
    const box = pin.getBoundingClientRect();
    const x = box.x + box.width / 2, y = box.y + box.height / 2;
    if (x < map.left + 40 || x > map.right - 80) continue;
    if (y < map.top + 180 || y > map.bottom - 140) continue;
    if (panel && x < panel.right + 20) continue;
    // Skip a pin a cluster badge (or an open popup) overlaps - hovering there opens the wrong card.
    const hit = document.elementFromPoint(x, y);
    if (!hit || !pin.contains(hit)) continue;
    return { x, y };
  }
  return null;
}
"""


# Set the origin of a Google Maps directions URL the element opens (see MAPS_ORIGIN): rewrites an
# anchor's href, and wraps window.open for buttons (plan.ts's "Open in Google Maps").
PIN_ORIGIN_JS = """
(element, origin) => {
  const pin = (href) => {
    const url = new URL(href, location.href);
    if (!url.hostname.endsWith('google.com') || !url.pathname.startsWith('/maps/dir')) return href;
    url.searchParams.set('origin', origin);
    return url.toString();
  };
  if (element instanceof HTMLAnchorElement) element.href = pin(element.href);
  if (!window.__tutOpenPinned) {
    const open = window.open.bind(window);
    window.open = (href, ...rest) => open(href === undefined ? href : pin(String(href)), ...rest);
    window.__tutOpenPinned = true;
  }
}
"""


def visible_cluster(page: Page) -> Locator | None:
    """The first precise-observation cluster badge clear of the results panel, or None."""
    panel = page.locator("#dock").bounding_box()
    badges = page.locator(".precise-cluster-icon")
    for index in range(badges.count()):
        box = badges.nth(index).bounding_box()
        height = (page.viewport_size or {"height": 760})["height"]
        if box and (panel is None or box["x"] > panel["x"] + panel["width"] + 20) and 180 < box["y"] < height - 200:
            return badges.nth(index)
    return None


def show_finds(tut: Tutorial, *, what: str, follow: tuple[str, ...] = ()) -> None:
    """From a selected destination: pins -> hover a cluster's list -> zoom -> one find's popup.

    `what` names the finds in the captions ("finds", "chanterelle finds"). `follow` opens the
    find's links and shows the page each one lands on: "inaturalist", "directions".
    """
    page = tut.page
    page.locator(".precise-cluster-icon").first.wait_for(timeout=30_000)
    time.sleep(2.0)
    tut.caption(
        f"<b>Ochre pins</b> are research-grade {what} with a verified location.",
        shot="pins",
        hold=3,
    )

    cluster = visible_cluster(page)
    if cluster is None:
        raise RuntimeError("no precise-observation cluster on screen to demonstrate")
    tut.point(cluster)
    page.locator(".cluster-popup").wait_for(timeout=10_000)
    time.sleep(0.8)
    tut.caption(
        "A numbered pin groups nearby finds. <b>Hover</b> it to list them, newest first.",
        shot="cluster-list",
        hold=3.2,
    )

    tut.caption("<b>Click</b> a numbered pin to zoom in until single finds separate out.", hold=1.4)
    # Clicking a badge zooms into it; repeat until single pins separate out.
    pin = page.evaluate(FIND_PIN_JS)
    for _ in range(4):
        if pin:
            break
        cluster = visible_cluster(page)
        if cluster is None:
            break
        tut.click(cluster, pause=2.2)
        pin = page.evaluate(FIND_PIN_JS)
    if not pin:
        raise RuntimeError("no single precise-observation pin on screen to demonstrate")
    page.mouse.move(pin["x"], pin["y"], steps=18)
    find_link = page.locator(".leaflet-popup-content a").filter(has_text="iNaturalist")
    try:
        find_link.wait_for(timeout=3_000)
    except PlaywrightTimeoutError:
        # The glide crossed a cluster badge and its list now covers the pin: step off, let the
        # list close, then jump straight onto the pin.
        page.mouse.move(pin["x"] + 300, pin["y"] + 200, steps=4)
        time.sleep(0.8)
        page.mouse.move(pin["x"], pin["y"])
        find_link.wait_for(timeout=10_000)
    time.sleep(0.8)
    tut.caption(
        "Hover a single find for its date, the full record on <b>iNaturalist ↗</b>, and "
        "<b>Directions</b> straight to the spot.",
        shot="find-popup",
        hold=3.6,
    )
    captions = {
        "inaturalist": ("iNaturalist", "The full record on <b>iNaturalist</b>: photos, notes and who ID'd it."),
        "directions": ("Directions", "<b>Directions</b> opens Google Maps, routed to the spot."),
    }
    for key in follow:
        link_text, caption = captions[key]
        link = page.locator(".leaflet-popup-content a").filter(has_text=link_text)
        if not link.is_visible():
            # The pointer stays on the popup while the other tab records, but re-hover the pin
            # in case the popup closed meanwhile.
            page.mouse.move(pin["x"], pin["y"])
            link.wait_for(timeout=10_000)
        tut.follow_link(link, caption)
    page.mouse.move(pin["x"] + 300, pin["y"], steps=8)
    time.sleep(0.5)


def best_spot(tut: Tutorial) -> None:
    page = tut.page
    tut.caption("No particular target? Find the <b>best spot right now</b> for anything fruiting.", hold=2.6)
    card = page.locator("#panel .rank").first
    tut.point(card.locator(".why"))
    tut.caption(
        "With <b>All genera</b> and <b>Best overall</b>, #1 has the strongest score for this month.",
        shot="ranked",
        hold=3.2,
    )
    tut.point(card.locator(".chips"))
    tut.caption(
        "Its chips show what's there: genus, share of the season in your months, record count.",
        shot="chips",
        hold=3.2,
    )
    tut.click(card.locator("h3"), pause=1.0)
    show_finds(tut, what="finds", follow=("inaturalist",))

    chip = card.locator(".chips .chip").first
    tut.point(chip)
    tut.caption("Each genus chip opens its iNaturalist page: photos, range and lookalikes.", hold=2.2)
    tut.follow_link(chip, "The genus on <b>iNaturalist</b>: photos, range and similar species.")

    sort = page.locator("#pills .pill-wrap").nth(0).locator(".pill")
    tut.click(sort)
    tut.click(page.locator("#pills .pill-popover button").filter(has_text="Active now").first)
    wait_for_results(page)
    tut.caption(
        "Sort by <b>Active now</b> for what's been seen in the last few weeks, with counts and dates.",
        shot="active-now",
        hold=3,
    )
    live = page.locator("#panel .rank").first.locator("a.chip.live").first
    if live.count():
        tut.point(live)
    tut.caption("Each of those chips opens that exact observation on iNaturalist.", shot="live-chip", hold=3)
    if live.count():
        tut.follow_link(live, "That sighting on <b>iNaturalist</b>, with its date and photos.")


def track_down(tut: Tutorial) -> None:
    page = tut.page
    tut.caption(
        "Got a target? Track it down from <b>where</b> to <b>which trail</b> to <b>where to sleep</b>.", hold=2.8
    )
    genera = page.locator("#pills .pill-wrap").nth(3).locator(".pill")
    tut.click(genera)
    tut.type_slowly(page.locator("#genus"), "Cantharellus")
    suggestion = page.locator("#genus-suggestions li").first
    suggestion.wait_for(timeout=15_000)
    tut.click(suggestion, pause=1.0)
    page.keyboard.press("Escape")
    wait_for_results(page)
    card = page.locator("#panel .rank").first
    tut.point(card.locator(".why"))
    tut.caption(
        "<b>1. Pick your target</b> under Genera. The list now ranks spots for chanterelles only.",
        shot="target",
        hold=3.2,
    )

    tut.click(card.locator('[data-act="details"]'), pause=1.5)
    tut.caption(
        "<b>2. Check the season.</b> Calendar shows which months chanterelles turn up here.",
        shot="season",
        hold=3.2,
    )
    tut.click(page.locator("#panel .details-back"), pause=1.2)

    tut.click(page.locator("#panel .rank").first.locator("h3"), pause=1.0)
    tut.caption("<b>3. See the finds.</b> Select the spot; the pins are chanterelles only.", hold=2)
    show_finds(tut, what="chanterelle finds", follow=("directions",))

    tut.click(page.locator("#panel .rank").first.locator('[data-act="details"]'), pause=1.5)
    tut.click(page.locator('#panel [data-tab="trails"]'), pause=1.0)
    chip = page.locator('#panel [data-tab-content="trails"] .chip').first
    chip.wait_for(timeout=30_000)
    time.sleep(1.0)
    tut.point(chip)
    tut.caption(
        "<b>4. Pick a trail.</b> Trails with chanterelle finds close by get a boost in the ranking.",
        shot="trails",
        hold=3.2,
    )
    tut.click(chip, pause=2.5)
    tut.caption("Select it to draw the trail on the map, right through the finds.", shot="trail-drawn", hold=3)

    tut.click(page.locator('#panel [data-tab="camps"]'), pause=1.0)
    camp = page.locator('#panel [data-tab-content="camps"] .chip').first
    camp.wait_for(timeout=30_000)
    time.sleep(1.0)
    tut.click(camp, pause=1.5)
    tut.caption(
        "<b>5. Find a camp.</b> Free sites are listed first, then the nearest.",
        shot="camp",
        hold=3,
    )
    pin = page.locator("#panel .pin-action:visible").first
    pin.wait_for(timeout=10_000)
    tut.click(pin, pause=1.0)
    tut.click(page.locator("#panel .details-back"), pause=1.2)
    # Pinning a camp already adds the spot to the trip ("✓ In route"); clicking + Plan again
    # would toggle it back out and leave the planner to auto-pick a trip instead.
    plan_button = page.locator("#panel .rank").first.locator('[data-act="plan"]')
    if "In route" in plan_button.inner_text():
        tut.point(plan_button)
    else:
        tut.click(plan_button, pause=0.8)
    tut.click(page.locator("#route-bar .route-bar-go"), pause=1.0)
    page.locator("#panel .stop-card").first.wait_for(timeout=60_000)
    time.sleep(2.5)
    tut.point(page.locator("#export-gmaps"))
    tut.caption(
        "<b>6. Go.</b> Plan the trip to that camp and open it in <b>Google Maps</b>, or export GPX.",
        shot="go",
        hold=3.4,
    )
    tut.follow_link(page.locator("#export-gmaps"), "The whole trip in <b>Google Maps</b>, stop by stop.")


def region_details(tut: Tutorial) -> None:
    page = tut.page
    layers = page.locator("#pills .pill-wrap").nth(4).locator(".pill")
    tut.caption("<b>Layers</b> adds land ownership, camps, fire and aerial imagery to the map.", hold=1.4)
    tut.click(layers)
    tut.caption("Turn on <b>USFS</b> and <b>BLM</b> to see where the public land is.", shot="layers")
    for toggle in ("#show-land-usfs", "#show-land-blm"):
        tut.click(page.locator(f"label:has({toggle})"), pause=0.8)
    page.keyboard.press("Escape")
    time.sleep(3)
    tut.caption("Forest Service and BLM land now shades the map.", shot="land-layer", hold=3)

    card = page.locator("#panel .rank").first
    tut.caption("Every destination card has a <b>Details</b> button.", hold=1.6)
    tut.click(card.locator('[data-act="details"]'), pause=1.5)
    tut.caption("<b>Calendar</b>: which genera fruit here, month by month.", shot="calendar", hold=3)

    for tab, text, shot in (
        ("photos", "<b>Photos</b>: recent research-grade iNaturalist finds from this spot.", "photos"),
        ("trails", "<b>Trails</b>: hiking paths, forest roads and trailheads nearby.", "trails"),
        ("camps", "<b>Campgrounds</b>: free sites first, then nearest.", "camps"),
        ("land", "<b>Public land</b>: who manages the ground nearby.", "land"),
    ):
        tut.click(page.locator(f'#panel [data-tab="{tab}"]'), pause=1.0)
        if tab == "photos":
            page.locator('#panel [data-tab-content="photos"] img').first.wait_for(timeout=30_000)
            time.sleep(2.5)  # let the thumbnails decode
        else:
            time.sleep(1.0)
        tut.caption(text, shot=shot, hold=2.8)

    tut.click(page.locator('#panel [data-tab="trails"]'), pause=1.5)
    # Required, like the Photos wait: the guide embeds this step's screenshot, so sparse data
    # should fail the run loudly rather than leave README.md pointing at a missing image.
    chip = page.locator('#panel [data-tab-content="trails"] .chip').first
    chip.wait_for(timeout=30_000)
    tut.click(chip, pause=2.5)
    tut.caption("Click a trail to draw it on the map, with the finds along it.", shot="trail-selected", hold=3)
    tut.click(page.locator("#panel .details-back"), pause=1.5)
    tut.caption("<b>← Back</b> returns to the ranked list.", hold=2)


def plan_a_trip(tut: Tutorial) -> None:
    page = tut.page
    cards = page.locator("#panel .rank")
    tut.caption("Build a road trip by shortlisting destinations with <b>+ Plan</b>.", hold=2)
    for index in range(3):
        tut.click(cards.nth(index).locator('[data-act="plan"]'), pause=0.9)
    tut.caption("Three spots picked. They collect in the bar at the bottom.", shot="shortlist", hold=2.6)

    tut.click(cards.nth(0).locator('[data-act="details"]'), pause=1.5)
    tut.click(page.locator('#panel [data-tab="camps"]'), pause=2.0)
    # Required (see the trail step in region_details): the guide embeds the pin screenshot.
    chip = page.locator('#panel [data-tab-content="camps"] .chip').first
    chip.wait_for(timeout=30_000)
    tut.click(chip, pause=1.2)
    pin = page.locator("#panel .pin-action:visible").first
    pin.wait_for(timeout=10_000)
    tut.caption("Optional: pick a campground and make it that stop's exact point.", hold=2)
    tut.click(pin, pause=1.0)
    tut.caption("📍 Pinned. The route will drive to this campground.", shot="pin", hold=2.4)
    tut.click(page.locator("#panel .details-back"), pause=1.5)

    tut.click(page.locator("#route-bar .route-bar-go"), pause=1.0)
    page.locator("#panel .stop-card").first.wait_for(timeout=60_000)
    time.sleep(2.5)
    tut.caption(
        "<b>Plan</b> orders your stops into a route, with a camp and trail at each.",
        shot="route",
        hold=3.2,
    )
    tut.point(page.locator("#panel .stop-card").nth(1))
    tut.caption("Each stop shows drive distance, what's fruiting, and where to sleep.", shot="stop", hold=3)
    tut.point(page.locator("#export-gpx"))
    tut.caption(
        "Export to <b>GPX</b> for any maps app, or open the route in <b>Google Maps</b>.",
        shot="export",
        hold=3,
    )
    tut.follow_link(page.locator("#export-gmaps"), "<b>Open in Google Maps</b> hands the route to Maps for the drive.")


def mobile(tut: Tutorial) -> None:
    page = tut.page
    tut.caption("On a phone the map fills the screen; results live in a <b>sheet</b>.", shot="map", hold=2.6)
    handle = page.locator("#sheet-handle")
    box = handle.bounding_box()
    if box:
        start_x, start_y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(start_x, start_y, steps=10)
        page.mouse.down()
        page.mouse.move(start_x, 140, steps=25)
        page.mouse.up()
        time.sleep(1.2)
    tut.caption("Drag the handle up to see the ranked destinations.", shot="sheet-open", hold=2.6)
    card = page.locator("#panel .rank").first
    tut.click(card.locator('[data-act="details"]'), pause=1.5)
    tut.caption("<b>Details</b>, trails, camps and land all work the same as on desktop.", shot="details")
    tut.click(page.locator('#panel [data-tab="trails"]'), pause=2.0)
    tut.caption("Swipe through the tabs to find a trailhead near the action.", shot="trails", hold=2.6)
    tut.click(page.locator("#panel .details-back"), pause=1.2)
    tut.click(card.locator('[data-act="plan"]'), pause=0.8)
    tut.click(page.locator("#panel .rank").nth(1).locator('[data-act="plan"]'), pause=0.8)
    tut.caption("<b>+ Plan</b> builds your trip from the sheet too.", shot="plan", hold=2.4)


# --- phone-layout walkthroughs (Instagram Reels) -------------------------------------------
# The desktop walkthroughs hover pins and use the side dock; a Reel is watched on a phone, so
# these two retell the same stories in the phone layout: a bottom sheet over a full-screen map,
# everything by tap.

# One single precise-observation pin well clear of the floating controls (top) and the sheet
# (bottom), or None. Taps need no hover, so unlike FIND_PIN_JS this only avoids overlap.
FIND_PHONE_PIN_JS = """
() => {
  for (const pin of document.querySelectorAll('#map .precise-pin-icon')) {
    const box = pin.getBoundingClientRect();
    const x = box.x + box.width / 2, y = box.y + box.height / 2;
    if (x < 50 || x > innerWidth - 50 || y < 300 || y > 520) continue;
    const hit = document.elementFromPoint(x, y);
    if (!hit || !pin.contains(hit)) continue;
    return { x, y };
  }
  return null;
}
"""

# Cluster badges clear of the controls and the sheet, smallest first: a small cluster opens a short
# list and takes fewer zooms to break up.
PHONE_CLUSTERS_JS = """
() => [...document.querySelectorAll('.precise-cluster-icon')]
  .map((badge) => {
    const box = badge.getBoundingClientRect();
    return { index: [...document.querySelectorAll('.precise-cluster-icon')].indexOf(badge),
             x: box.x + box.width / 2, y: box.y + box.height / 2,
             count: Number(badge.textContent.trim().replace(/[^0-9]/g, '')) || 0 };
  })
  .filter((entry) => entry.count >= 2 && entry.x > 50 && entry.x < innerWidth - 50
                     && entry.y > 330 && entry.y < 520)
  .sort((left, right) => left.count - right.count)
"""


CENTER_JS = "(element) => element.scrollIntoView({ block: 'center' })"


def drag_sheet(tut: Tutorial, to_y: float) -> None:
    """Drag the bottom sheet's handle to `to_y` (CSS px from the top): ~140 raises it, ~700 drops it."""
    page = tut.page
    box = page.locator("#sheet-handle").bounding_box()
    if box is None:
        return
    start_x, start_y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    glide(page, start_x, start_y, steps=8)
    page.mouse.down()
    glide(page, start_x, to_y, steps=30)
    page.mouse.up()
    time.sleep(1.2)


def pan_map(tut: Tutorial, delta_y: float) -> None:
    """Drag the map down by `delta_y` px (positive moves what's on screen downward)."""
    page = tut.page
    glide(page, 200, 330, steps=4)
    page.mouse.down()
    glide(page, 200, 330 + delta_y, steps=30)
    page.mouse.up()
    time.sleep(1.5)


def show_finds_phone(tut: Tutorial, *, what: str, follow: tuple[str, ...] = ()) -> None:
    """Phone version of show_finds: tap a numbered pin for its list, "Zoom in" down to one find,
    tap it for its popup. The sheet starts raised over the selected destination's card."""
    page = tut.page
    page.locator(".precise-cluster-icon").first.wait_for(timeout=30_000)
    drag_sheet(tut, 700)
    # Selecting a card centres the destination in the strip above the raised sheet; with the sheet
    # lowered that strip is the top of the screen, behind the controls, so bring it down.
    middle = page.evaluate(
        "() => { const ys = [...document.querySelectorAll('.precise-cluster-icon, .precise-pin-icon')]"
        ".map((el) => { const box = el.getBoundingClientRect(); return box.y + box.height / 2; });"
        " return ys.length ? ys.reduce((total, value) => total + value, 0) / ys.length : null; }"
    )
    if middle is not None:
        pan_map(tut, 430 - middle)
    time.sleep(1.0)
    tut.caption(
        f"<b>Ochre pins</b> are research-grade {what} with a verified location.",
        shot="pins",
        hold=3,
    )

    clusters = page.evaluate(PHONE_CLUSTERS_JS)
    if not clusters:
        raise RuntimeError("no precise-observation cluster on screen to demonstrate")
    page.locator(".precise-cluster-icon").nth(clusters[0]["index"]).tap()
    page.locator(".cluster-popup").wait_for(timeout=10_000)
    time.sleep(0.8)
    tut.caption(
        "A numbered pin groups nearby finds. <b>Tap</b> it to list them, newest first.",
        shot="cluster-list",
        hold=3.2,
    )
    tut.caption("Tap <b>Zoom in</b> until single finds separate out.", hold=1.4)
    pin = None
    for _ in range(8):
        zoom = page.locator(".cluster-list-zoom")
        if zoom.count():
            zoom.first.tap()
            time.sleep(2.2)
        pin = page.evaluate(FIND_PHONE_PIN_JS)
        if pin:
            break
        clusters = page.evaluate(PHONE_CLUSTERS_JS)
        if not clusters:
            break
        page.locator(".precise-cluster-icon").nth(clusters[0]["index"]).tap()
        page.locator(".cluster-popup").wait_for(timeout=10_000)
        time.sleep(0.8)
    if not pin:
        raise RuntimeError("no single precise-observation pin on screen to demonstrate")
    page.mouse.click(pin["x"], pin["y"])
    find_link = page.locator(".leaflet-popup-content a").filter(has_text="iNaturalist")
    find_link.wait_for(timeout=10_000)
    time.sleep(2.5)  # let the photo load
    tut.caption(
        "Tap a single find for its photo, date, the full record on <b>iNaturalist ↗</b> and "
        "<b>Directions</b> straight to the spot.",
        shot="find-popup",
        hold=3.6,
    )
    captions = {
        "inaturalist": ("iNaturalist", "The full record on <b>iNaturalist</b>: photos, notes and who ID'd it."),
        "directions": ("Directions", "<b>Directions</b> opens Google Maps, routed to the spot."),
    }
    for key in follow:
        link_text, caption = captions[key]
        link = page.locator(".leaflet-popup-content a").filter(has_text=link_text)
        time.sleep(1.0)  # the viewport restore after the carousel still can close the popup
        try:
            link.wait_for(timeout=2_000)
        except PlaywrightTimeoutError:
            page.mouse.click(pin["x"], pin["y"])
            link.wait_for(timeout=10_000)
        time.sleep(1.5)  # let the photo load
        tut.follow_link(link, caption)
    page.keyboard.press("Escape")
    time.sleep(0.5)


def best_spot_phone(tut: Tutorial) -> None:
    page = tut.page
    tut.caption("No particular target? Find the <b>best spot right now</b> for anything fruiting.", hold=2.6)
    drag_sheet(tut, 300)
    card = page.locator("#panel .rank").first
    tut.point(card.locator(".why"))
    tut.caption(
        "With <b>All genera</b> and <b>Best overall</b>, #1 has the strongest score for this month.",
        shot="ranked",
        hold=3.2,
    )
    tut.point(card.locator(".chips"))
    tut.caption(
        "Its chips show what's there: genus, share of the season in your months, record count.",
        shot="chips",
        hold=3.2,
    )
    tut.click(card.locator("h3"), pause=1.5)
    show_finds_phone(tut, what="finds", follow=("inaturalist",))

    drag_sheet(tut, 300)
    chip = page.locator("#panel .rank").first.locator(".chips .chip").first
    tut.point(chip)
    tut.caption("Each genus chip opens its iNaturalist page: photos, range and lookalikes.", hold=2.2)
    tut.follow_link(chip, "The genus on <b>iNaturalist</b>: photos, range and similar species.")

    sort = page.locator("#pills .pill-wrap").nth(0).locator(".pill")
    tut.click(sort)
    tut.click(page.locator("#pills .pill-popover button").filter(has_text="Active now").first)
    wait_for_results(page)
    drag_sheet(tut, 300)
    tut.caption(
        "Sort by <b>Active now</b> for what's been seen in the last few weeks, with counts and dates.",
        shot="active-now",
        hold=3,
    )
    live = page.locator("#panel .rank").first.locator("a.chip.live").first
    if live.count():
        tut.point(live)
    tut.caption("Each of those chips opens that exact observation on iNaturalist.", shot="live-chip", hold=3)
    if live.count():
        tut.follow_link(live, "That sighting on <b>iNaturalist</b>, with its date and photos.")


def track_down_phone(tut: Tutorial) -> None:
    page = tut.page
    tut.caption(
        "Got a target? Track it down from <b>where</b> to <b>which trail</b> to <b>where to sleep</b>.", hold=2.8
    )
    genera = page.locator("#pills .pill-wrap").nth(3).locator(".pill")
    tut.click(genera)
    tut.type_slowly(page.locator("#genus"), "Cantharellus")
    suggestion = page.locator("#genus-suggestions li").first
    suggestion.wait_for(timeout=15_000)
    tut.click(suggestion, pause=1.0)
    page.keyboard.press("Escape")
    wait_for_results(page)
    drag_sheet(tut, 300)
    card = page.locator("#panel .rank").first
    tut.point(card.locator(".why"))
    tut.caption(
        "<b>1. Pick your target</b> under Genera. The list now ranks spots for chanterelles only.",
        shot="target",
        hold=3.2,
    )

    tut.click(card.locator('[data-act="details"]'), pause=1.5)
    tut.caption(
        "<b>2. Check the season.</b> Calendar shows which months chanterelles turn up here.",
        shot="season",
        hold=3.2,
    )
    tut.click(page.locator("#panel .details-back"), pause=1.2)

    tut.click(page.locator("#panel .rank").first.locator("h3"), pause=1.5)
    tut.caption("<b>3. See the finds.</b> Select the spot; the pins are chanterelles only.", hold=2)
    show_finds_phone(tut, what="chanterelle finds", follow=("directions",))

    drag_sheet(tut, 300)
    tut.click(page.locator("#panel .rank").first.locator('[data-act="details"]'), pause=1.5)
    tut.click(page.locator('#panel [data-tab="trails"]'), pause=1.0)
    chip = page.locator('#panel [data-tab-content="trails"] .chip').first
    chip.wait_for(timeout=30_000)
    time.sleep(1.0)
    tut.point(chip)
    tut.caption(
        "<b>4. Pick a trail.</b> Trails with chanterelle finds close by get a boost in the ranking.",
        shot="trails",
        hold=3.2,
    )
    tut.click(chip, pause=2.5)
    tut.caption("Select it to draw the trail on the map, right through the finds.", shot="trail-drawn", hold=3)

    tut.click(page.locator('#panel [data-tab="camps"]'), pause=1.0)
    camp = page.locator('#panel [data-tab-content="camps"] .chip').first
    camp.wait_for(timeout=30_000)
    time.sleep(1.0)
    tut.click(camp, pause=1.5)
    tut.caption(
        "<b>5. Find a camp.</b> Free sites are listed first, then the nearest.",
        shot="camp",
        hold=3,
    )
    pin = page.locator("#panel .pin-action:visible").first
    pin.wait_for(timeout=10_000)
    drag_sheet(tut, 140)
    pin.evaluate(CENTER_JS)
    tut.point(pin)
    pin.evaluate("(element) => element.click()")  # the fixed route bar can sit over the real tap point
    time.sleep(1.0)
    tut.click(page.locator("#panel .details-back"), pause=1.2)
    plan_button = page.locator("#panel .rank").first.locator('[data-act="plan"]')
    plan_button.evaluate(CENTER_JS)  # the fixed route bar would cover it at the sheet's edge
    if "In route" in plan_button.inner_text():
        tut.point(plan_button)
    else:
        tut.click(plan_button, pause=0.8)
    tut.click(page.locator("#route-bar .route-bar-go"), pause=1.0)
    page.locator("#panel .stop-card").first.wait_for(timeout=60_000)
    time.sleep(2.5)
    page.locator("#export-gmaps").evaluate(CENTER_JS)
    tut.point(page.locator("#export-gmaps"))
    tut.caption(
        "<b>6. Go.</b> Plan the trip to that camp and open it in <b>Google Maps</b>, or export GPX.",
        shot="go",
        hold=3.4,
    )
    tut.follow_link(page.locator("#export-gmaps"), "The whole trip in <b>Google Maps</b>, stop by stop.")


TUTORIALS: dict[str, tuple[Callable[[Tutorial], None], bool]] = {
    "getting-started": (getting_started, False),
    "best-spot": (best_spot, False),
    "track-down": (track_down, False),
    "region-details": (region_details, False),
    "plan-a-trip": (plan_a_trip, False),
    "mobile": (mobile, True),
}


# Reels are watched on a phone, so `--instagram` swaps these desktop walkthroughs for their
# phone-layout retellings (same slugs, same story, tap instead of hover).
PHONE_REELS: dict[str, Callable[[Tutorial], None]] = {
    "best-spot": best_spot_phone,
    "track-down": track_down_phone,
}


class Screencast:
    """Lossless frames from Chrome's screencast, which only emits when the page repaints.

    A recorded .webm's compression noise makes every frame differ, so a GIF made from it
    can't reuse unchanged pixels between frames; PNG frames + their timestamps keep the
    still stretches (caption holds) nearly free.
    """

    def __init__(self, page: Page, frame_dir: Path, profile: Profile) -> None:
        self.frame_dir = frame_dir
        self.profile = profile
        self.frame_size = (
            round(profile.viewport["width"] * profile.scale),
            round(profile.viewport["height"] * profile.scale),
        )
        self.frames: list[tuple[float, Path]] = []
        # While set, frames are acked but dropped: the last kept frame holds over the gap.
        self.paused = False
        self.running = False
        self.session = self._attach(page)
        self.stopped_at = 0.0

    def _attach(self, page: Page) -> CDPSession:
        session = page.context.new_cdp_session(page)
        session.on("Page.screencastFrame", lambda event: self._on_frame(session, event))
        return session

    def _on_frame(self, session: CDPSession, event: dict) -> None:
        data = base64.b64decode(event["data"])
        # PNG IHDR: width/height are the big-endian uint32s at bytes 16-24. A frame rendered
        # mid-resize (carousel still) can land after unpausing; anything off-size is dropped.
        size = (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
        if self.paused or size != self.frame_size:
            session.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
            return
        path = self.frame_dir / f"{len(self.frames):05d}.png"
        path.write_bytes(data)
        self.frames.append((event["metadata"]["timestamp"], path))
        session.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})

    def switch_to(self, page: Page) -> None:
        """Move the recording to another tab (Tutorial.follow_link) and keep the same timeline."""
        was_running = self.running
        if was_running:
            self.session.send("Page.stopScreencast")
        self.session.detach()
        self.session = self._attach(page)
        if was_running:
            self.start()

    def start(self) -> None:
        self.running = True
        # Frames only come at device resolution when the browser itself runs at that scale
        # (--force-device-scale-factor, see record()); max* just has to not cap them.
        self.session.send(
            "Page.startScreencast",
            {
                "format": "png",
                "everyNthFrame": 1,
                "maxWidth": self.frame_size[0],
                "maxHeight": self.frame_size[1],
            },
        )

    def stop(self) -> None:
        self.stopped_at = time.time()
        self.running = False
        self.session.send("Page.stopScreencast")

    def _concat_list(self) -> Path:
        concat = self.frame_dir / "frames.txt"
        lines = []
        # A tab switch can deliver the old tab's last frames after the new tab's first ones.
        frames = sorted(self.frames)
        for index, (stamp, path) in enumerate(frames):
            following = frames[index + 1][0] if index + 1 < len(frames) else self.stopped_at
            lines += [f"file '{path.name}'", f"duration {max(following - stamp, 0.01):.3f}"]
        lines.append(f"file '{frames[-1][1].name}'")
        concat.write_text("\n".join(lines) + "\n")
        return concat

    def to_mp4(self, target: Path) -> None:
        """H.264 1080x1920 at 30 fps, the shape and codec Instagram Reels expect."""
        command = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-i", str(self._concat_list())]
        encode = ["-vf", "fps=30,scale=1080:1920:flags=lanczos,format=yuv420p", "-c:v", "libx264"]
        encode += ["-preset", "slow", "-crf", "18", "-profile:v", "high", "-movflags", "+faststart", "-an"]
        subprocess.run([*command, *encode, str(target)], check=True)

    def to_gif(self, target: Path) -> None:
        concat = self._concat_list()
        filters = (
            # Native resolution (no downscale keeps UI text crisp), 15 fps, full 256-colour palette.
            "fps=15,mpdecimate=hi=1:lo=1:frac=1,split[a][b];"
            "[a]palettegen=max_colors=256:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle"
        )
        command = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-i", str(concat)]
        subprocess.run([*command, "-vf", filters, "-fps_mode", "vfr", str(target)], check=True)


def record(playwright: Playwright, url: str, slug: str, *, headed: bool, instagram: bool) -> Tutorial:
    run, is_mobile = TUTORIALS[slug]
    if instagram and slug in PHONE_REELS:
        run, is_mobile = PHONE_REELS[slug], True
    profile = PROFILES[instagram, is_mobile]
    out_dir = INSTAGRAM_DIR if instagram else OUT_DIR
    img_dir = INSTAGRAM_DIR / "stills" if instagram else IMG_DIR
    img_dir.mkdir(parents=True, exist_ok=True)
    for stale in img_dir.glob(f"{slug}-*.*"):
        stale.unlink()
    # One browser per tutorial: the scale flag is browser-wide, and without it the screencast
    # hands back CSS-pixel frames however high the context's device_scale_factor is.
    browser = playwright.chromium.launch(
        headless=not headed,
        args=["--use-gl=angle", f"--force-device-scale-factor={profile.scale}"],
    )
    context = browser.new_context(
        viewport=profile.viewport,
        device_scale_factor=profile.scale,
        is_mobile=profile.mobile,
        has_touch=profile.mobile,
        color_scheme="dark",
        service_workers="block",
    )
    page = context.new_page()
    page.goto(url)
    wait_for_results(page)
    if slug != "getting-started":
        set_home_quietly(page)
    with tempfile.TemporaryDirectory() as frame_dir:
        cast = Screencast(page, Path(frame_dir), profile)
        tut = Tutorial(page=page, slug=slug, img_dir=img_dir, on_start=cast.start, cast=cast, touch=profile.mobile)
        if instagram:
            tut.carousel_viewport = CAROUSEL_VIEWPORTS[is_mobile]
            tut.carousel_dir = INSTAGRAM_DIR / "carousel-raw"
            tut.carousel_dir.mkdir(parents=True, exist_ok=True)
        try:
            run(tut)
        except Exception:
            failed = Path(tempfile.gettempdir()) / f"tutorial-{slug}-failed.png"
            page.screenshot(path=failed)
            print(f"{slug}: failed - page at the time of failure saved to {failed}")
            raise
        cast.stop()
        context.close()
        browser.close()
        if instagram:
            cast.to_mp4(out_dir / f"{slug}.mp4")
            to_carousel(tut)
        else:
            cast.to_gif(out_dir / f"{slug}.gif")
    return tut


def to_carousel(tut: Tutorial) -> None:
    """Each step's 4:5 still as a 1080x1350 JPEG, ready for a feed carousel post."""
    carousel = INSTAGRAM_DIR / "carousel"
    carousel.mkdir(parents=True, exist_ok=True)
    for stale in carousel.glob(f"{tut.slug}-*.*"):
        stale.unlink()
    assert tut.carousel_dir is not None
    for name, _caption in tut.steps:
        source = tut.carousel_dir / name
        target = carousel / Path(name).with_suffix(".jpg").name
        command = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-vf", "scale=1080:1350:flags=lanczos"]
        subprocess.run([*command, "-q:v", "2", str(target)], check=True)
        source.unlink()


def set_home_quietly(page: Page) -> None:
    """Point a non-intro tutorial at the same home the intro sets, before recording starts."""
    box = page.locator("#loc")
    for _ in range(3):
        box.fill("")
        box.press_sequentially("Bend, Oregon", delay=30)
        try:
            page.locator("#loc-suggestions li").first.click(timeout=15_000)
            break
        except PlaywrightTimeoutError:
            continue  # the geocoder can be slow / rate-limited; retype to re-query
    else:
        box.press("Enter")  # free-text submit: the server geocodes it instead
    wait_for_home(page, "Bend")
    wait_for_results(page)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("slugs", nargs="*", help=f"tutorials to record (default: all of {', '.join(TUTORIALS)})")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--instagram",
        action="store_true",
        help=f"record 1080x1920 MP4 Reels + 4:5 carousel stills into {INSTAGRAM_DIR.name}/ "
        f"(default tutorials: {', '.join(INSTAGRAM_DEFAULT)})",
    )
    args = parser.parse_args()
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is required")
    slugs = args.slugs or (INSTAGRAM_DEFAULT if args.instagram else list(TUTORIALS))
    unknown = sorted(set(slugs) - set(TUTORIALS))
    if unknown:
        parser.error(f"unknown tutorial(s): {', '.join(unknown)}")
    with sync_playwright() as playwright:
        for slug in slugs:
            tut = record(playwright, args.url, slug, headed=args.headed, instagram=args.instagram)
            output = INSTAGRAM_DIR / f"{slug}.mp4" if args.instagram else OUT_DIR / f"{slug}.gif"
            print(f"{slug}: {len(tut.steps)} screenshots, {output}")
    raw = INSTAGRAM_DIR / "carousel-raw"
    if raw.is_dir() and not any(raw.iterdir()):
        raw.rmdir()


if __name__ == "__main__":
    main()
