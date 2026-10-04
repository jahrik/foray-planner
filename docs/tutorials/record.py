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
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# playwright comes from the inline script metadata above, not the project venv `just lint` checks.
from playwright.sync_api import Browser, Locator, Page, sync_playwright  # ty: ignore[unresolved-import]

OUT_DIR = Path(__file__).resolve().parent
IMG_DIR = OUT_DIR / "img"
DEFAULT_URL = "https://forayplanner.com/"

DESKTOP = {"width": 1280, "height": 760}
MOBILE = {"width": 390, "height": 760}

# Injected once per page: a caption bar pinned to the bottom and a cursor dot that follows
# the (synthetic) mouse - headless Chromium draws no pointer of its own.
OVERLAY_JS = """
() => {
  if (document.getElementById('tut-caption')) return;
  const style = document.createElement('style');
  style.textContent = `
    #tut-caption { position: fixed; left: 16px; right: 16px; bottom: 34px; margin: 0 auto;
      width: fit-content; z-index: 2147483647; max-width: 760px; padding: 10px 18px; border-radius: 10px;
      background: rgba(20, 16, 12, .92); color: #f6efe6; font: 600 17px/1.35 system-ui, sans-serif;
      box-shadow: 0 6px 24px rgba(0,0,0,.45); text-align: center; pointer-events: none;
      transition: opacity .2s; }
    #tut-caption[data-empty] { opacity: 0; }
    @media (max-width: 500px) { #tut-caption { font-size: 15px; bottom: 74px; padding: 8px 12px; } }
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


@dataclass
class Tutorial:
    page: Page
    slug: str
    mobile: bool = False
    on_start: Callable[[], None] | None = None
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
            self.page.screenshot(path=IMG_DIR / name, type="png", style="#tut-cursor { display: none; }")
            self.steps.append((name, html))
        time.sleep(hold)

    def point(self, target: Locator) -> None:
        """Glide the visible cursor onto `target` (tap targets on mobile just jump)."""
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        if box is None:
            return
        self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=18)
        time.sleep(0.25)

    def click(self, target: Locator, *, pause: float = 0.6) -> None:
        self.point(target)
        target.click()
        time.sleep(pause)

    def type_slowly(self, target: Locator, text: str) -> None:
        self.click(target)
        target.press_sequentially(text, delay=90)
        time.sleep(0.8)


def wait_for_results(page: Page) -> None:
    page.locator("#panel .rank").first.wait_for(timeout=60_000)
    time.sleep(2.5)  # let the map settle + markers paint


def set_home(tut: Tutorial, query: str) -> None:
    page = tut.page
    tut.type_slowly(page.locator("#loc"), query)
    suggestion = page.locator("#loc-suggestions li").first
    suggestion.wait_for(timeout=15_000)
    tut.click(suggestion, pause=1.0)
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
        "Each card leads with <b>why</b> it ranks: what's fruiting, how many records, recent rain.",
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
        ("camps", "<b>Campgrounds</b>: developed and free sites, closest first.", "camps"),
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
    chip = page.locator('#panel [data-tab-content="trails"] .chip').first
    if chip.count():
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
    chip = page.locator('#panel [data-tab-content="camps"] .chip').first
    pinned = False
    if chip.count():
        tut.click(chip, pause=1.2)
        pin = page.locator("#panel .pin-action:visible").first
        if pin.count():
            tut.caption("Optional: pick a campground and make it that stop's exact point.", hold=2)
            tut.click(pin, pause=1.0)
            tut.caption("📍 Pinned. The route will drive to this campground.", shot="pin", hold=2.4)
            pinned = True
    if not pinned:
        tut.caption("Details lists the campgrounds near each stop.", hold=2)
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


TUTORIALS: dict[str, tuple[Callable[[Tutorial], None], bool]] = {
    "getting-started": (getting_started, False),
    "region-details": (region_details, False),
    "plan-a-trip": (plan_a_trip, False),
    "mobile": (mobile, True),
}


class Screencast:
    """Lossless frames from Chrome's screencast, which only emits when the page repaints.

    A recorded .webm's compression noise makes every frame differ, so a GIF made from it
    can't reuse unchanged pixels between frames; PNG frames + their timestamps keep the
    still stretches (caption holds) nearly free.
    """

    def __init__(self, page: Page, frame_dir: Path) -> None:
        self.frame_dir = frame_dir
        self.frames: list[tuple[float, Path]] = []
        self.session = page.context.new_cdp_session(page)
        self.session.on("Page.screencastFrame", self._on_frame)
        self.stopped_at = 0.0

    def _on_frame(self, event: dict) -> None:
        path = self.frame_dir / f"{len(self.frames):05d}.png"
        path.write_bytes(base64.b64decode(event["data"]))
        self.frames.append((event["metadata"]["timestamp"], path))
        self.session.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})

    def start(self) -> None:
        self.session.send("Page.startScreencast", {"format": "png", "everyNthFrame": 1})

    def stop(self) -> None:
        self.stopped_at = time.time()
        self.session.send("Page.stopScreencast")

    def to_gif(self, target: Path) -> None:
        concat = self.frame_dir / "frames.txt"
        lines = []
        for index, (stamp, path) in enumerate(self.frames):
            following = self.frames[index + 1][0] if index + 1 < len(self.frames) else self.stopped_at
            lines += [f"file '{path.name}'", f"duration {max(following - stamp, 0.01):.3f}"]
        lines.append(f"file '{self.frames[-1][1].name}'")
        concat.write_text("\n".join(lines) + "\n")
        filters = (
            # Native resolution (no downscale keeps UI text crisp), 15 fps, full 256-colour palette.
            "fps=15,mpdecimate=hi=1:lo=1:frac=1,split[a][b];"
            "[a]palettegen=max_colors=256:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle"
        )
        command = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-i", str(concat)]
        subprocess.run([*command, "-vf", filters, "-fps_mode", "vfr", str(target)], check=True)


def record(browser: Browser, url: str, slug: str) -> Tutorial:
    run, is_mobile = TUTORIALS[slug]
    viewport = MOBILE if is_mobile else DESKTOP
    context = browser.new_context(
        viewport=viewport,
        device_scale_factor=1,
        is_mobile=is_mobile,
        has_touch=is_mobile,
        color_scheme="dark",
        service_workers="block",
    )
    page = context.new_page()
    page.goto(url)
    wait_for_results(page)
    if slug != "getting-started":
        set_home_quietly(page)
    with tempfile.TemporaryDirectory() as frame_dir:
        cast = Screencast(page, Path(frame_dir))
        tut = Tutorial(page=page, slug=slug, on_start=cast.start)
        run(tut)
        cast.stop()
        context.close()
        cast.to_gif(OUT_DIR / f"{slug}.gif")
    return tut


def set_home_quietly(page: Page) -> None:
    """Point a non-intro tutorial at the same home the intro sets, before recording starts."""
    page.locator("#loc").fill("Bend, Oregon")
    page.locator("#loc-suggestions li").first.click(timeout=15_000)
    wait_for_results(page)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("slugs", nargs="*", help=f"tutorials to record (default: all of {', '.join(TUTORIALS)})")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is required")
    IMG_DIR.mkdir(exist_ok=True)
    slugs = args.slugs or list(TUTORIALS)
    unknown = sorted(set(slugs) - set(TUTORIALS))
    if unknown:
        parser.error(f"unknown tutorial(s): {', '.join(unknown)}")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed, args=["--use-gl=angle"])
        for slug in slugs:
            for stale in IMG_DIR.glob(f"{slug}-*.*"):
                stale.unlink()
            tut = record(browser, args.url, slug)
            print(f"{slug}: {len(tut.steps)} screenshots, {OUT_DIR / (slug + '.gif')}")
        browser.close()


if __name__ == "__main__":
    main()
