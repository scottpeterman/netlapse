#!/usr/bin/env python3
"""
screenshots.py — regenerate the Forwarding and Topology documentation screenshots.

Builds the synthetic demo network (tools/demo_lab.py), starts netlapse on
it, drives the web UI in headless Chromium and writes PNGs (and optionally
animated GIFs) to screenshots/forwarding/ and screenshots/topology/, plus a
feature tour, screenshots/netlapse_tour.gif (with --gif, or --only tour).
Nothing real is shown: every hostname and address comes from the demo lab.

Needs Playwright (pip install playwright; playwright install chromium) and,
for --gif, ffmpeg. Where Google Fonts can't be reached (sandboxes, CI),
point --fonts-dir at a folder holding the IBM Plex Sans/Mono latin woff2
files (npm i @fontsource/ibm-plex-sans @fontsource/ibm-plex-mono gives them)
and the page gets them locally instead of falling back to system fonts.

    python tools/screenshots.py
    python tools/screenshots.py --only topology
    python tools/screenshots.py --only tour
    python tools/screenshots.py --gif --chrome /usr/bin/google-chrome --fonts-dir /tmp/fonts/node_modules/@fontsource
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSWORD = "demo-pass-1"
PORT = 8898
BASE = f"http://127.0.0.1:{PORT}"
FW_SITES = "site=den1,den2"   # Forwarding shots: the two Denver sites (ams1 is a separate island)


def font_css(fonts_dir: Path) -> tuple:
    """(@font-face CSS, {url: path}) for IBM Plex Sans/Mono from fontsource folders."""
    faces, files = [], {}
    for family, pkg, weights in (("IBM Plex Sans", "ibm-plex-sans", (400, 500, 600, 700)),
                                 ("IBM Plex Mono", "ibm-plex-mono", (400, 500, 600))):
        for w in weights:
            f = fonts_dir / pkg / "files" / f"{pkg}-latin-{w}-normal.woff2"
            if f.exists():
                url = f"https://fonts.local/{f.name}"
                files[url] = f
                faces.append(f"@font-face{{font-family:'{family}';font-style:normal;font-weight:{w};"
                             f"font-display:block;src:url({url}) format('woff2');}}")
    return "\n".join(faces), files


def wait_up(timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"{BASE}/api/v1/health", timeout=2)
            return
        except Exception as e:
            if getattr(e, "code", None) in (200, 401):
                return
            time.sleep(0.5)
    raise SystemExit("netlapse didn't come up; see the server log")


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)


def make_gif(video_path, gif: Path, width=1000, start=0.0):
    if not (video_path and shutil.which("ffmpeg")):
        return
    palette = gif.with_name("_palette.png")
    # Small enough to live in the repo: 4 fps, 64-color palette.
    vf = f"fps=4,scale={width}:-1:flags=lanczos"
    trim = ["-ss", f"{start:.2f}"] if start else []
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *trim, "-i", video_path, "-vf",
                    f"{vf},palettegen=max_colors=64:stats_mode=diff", str(palette)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *trim, "-i", video_path, "-i", str(palette), "-lavfi",
                    f"{vf} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
                    str(gif)], check=True)
    palette.unlink(missing_ok=True)
    print(f"  {rel(gif)}")


class Shooter:
    def __init__(self, args, browser, ctx, page, use_fonts, tmp_video: Path):
        self.args, self.browser, self.ctx, self.page = args, browser, ctx, page
        self.use_fonts, self.tmp_video = use_fonts, tmp_video

    async def set_theme(self, theme):
        # Through the app's own toggle: it stores the preference and reloads.
        page = self.page
        await page.goto(f"{BASE}/#/dashboard")
        await page.wait_for_selector("#btn-theme")
        is_dark = "dark" in (await page.get_attribute("#theme-css", "href") or "")
        if is_dark != (theme == "dark"):
            async with page.expect_navigation():
                await page.click("#btn-theme")
            await page.wait_for_selector("#btn-theme")

    async def snap(self, out: Path, name, locator=None, full=False):
        path = out / f"{name}.png"
        if locator is not None:
            await locator.scroll_into_view_if_needed()
            await locator.screenshot(path=str(path))
        else:
            await self.page.screenshot(path=str(path), full_page=full)
        print(f"  {rel(path)}")

    async def video_page(self, size):
        vctx = await self.browser.new_context(viewport=size, record_video_dir=str(self.tmp_video),
                                              record_video_size=size)
        await self.use_fonts(vctx)
        await vctx.add_cookies(await self.ctx.cookies())
        return vctx, await vctx.new_page()

    # ── Forwarding ──────────────────────────────────────────────────────

    async def forwarding(self, out: Path, then_iso: str):
        page = self.page

        async def go(params="", ready="#fw-mx td[data-k]"):
            q = FW_SITES + (("&" + params) if params else "")
            await page.goto(f"{BASE}/#/forwarding?{q}")
            await page.reload()
            await page.wait_for_selector(ready, timeout=120000)
            await page.evaluate("document.fonts.ready")
            await page.wait_for_timeout(600)

        def card(sel):
            return page.locator(sel).locator("xpath=ancestor-or-self::div[contains(@class,'card')][1]")

        # 1. overview: controls, cards, matrix
        await page.set_viewport_size({"width": 1440, "height": 1180})
        await go()
        await self.snap(out, "forwarding_overview")

        # 2. a cross-site pair, both directions (tall viewport: the card must fit
        #    under the fixed page header or the header lands in the shot)
        await page.set_viewport_size({"width": 1440, "height": 2000})
        await go("pair=leaf05.den2|leaf03.den1", "#fw-detail .fw-layers")
        await self.snap(out, "forwarding_pair", card("#fw-detail"))

        # 3. first-hop fanout: the single-homed leafs light up
        await page.set_viewport_size({"width": 1440, "height": 1200})
        await go("color=fanout")
        await self.snap(out, "forwarding_fanout", card("#fw-mx"))

        # 4. trace into the black hole
        await go("trace=leaf03.den1~10.20.12.10", "#fw-trace-out .fw-layers")
        await self.snap(out, "forwarding_trace_blackhole", card("#fw-trace-out"))

        # 5. trace the long way round: spine2.den1 to a leaf it lost its port to
        await go("trace=spine2.den1~leaf07.den1", "#fw-trace-out .fw-layers")
        await self.snap(out, "forwarding_trace_long_way", card("#fw-trace-out"))

        # 6. data quality and the per-endpoint summary
        await go()
        await self.snap(out, "forwarding_quality", card("#fw-quality"))
        await self.snap(out, "forwarding_summary", card("#fw-sum"))

        # 7. two days ago: leaf07/08 still dual-homed
        await go(f"as_of={then_iso}&color=fanout")
        await self.snap(out, "forwarding_fanout_as_of", card("#fw-mx"))

        # 8. dark theme
        await self.set_theme("dark")
        await page.set_viewport_size({"width": 1440, "height": 1180})
        await go("pair=leaf03.den1|leaf07.den1")
        await self.snap(out, "forwarding_dark")
        await self.set_theme("light")

        # 9. standalone HTML report
        await page.set_viewport_size({"width": 1440, "height": 1000})
        await page.goto(f"{BASE}/api/v1/forwarding/report?{FW_SITES}")
        await page.wait_for_selector("#mx td[data-k]")
        await page.evaluate("document.fonts.ready")
        await page.wait_for_timeout(400)
        await self.snap(out, "forwarding_report")

        # 10. optional walkthrough GIF, in its own context so the video holds
        #     only the walkthrough, at a size whose text survives downscaling
        if self.args.gif:
            vctx, vp = await self.video_page({"width": 1180, "height": 760})
            await vp.goto(f"{BASE}/#/forwarding?{FW_SITES}")
            await vp.wait_for_selector("#fw-mx td[data-k]", timeout=120000)
            await vp.evaluate("document.fonts.ready")
            await vp.wait_for_timeout(1200)
            for mode in ("asym", "fanout", "status"):
                await vp.select_option("#fw-mode", mode)
                await vp.wait_for_timeout(1300)
            await vp.click('#fw-mx td[data-k="leaf05.den2|leaf03.den1"]')
            await vp.wait_for_selector("#fw-detail .fw-layers")
            await vp.evaluate("document.getElementById('fw-detail').scrollIntoView({block: 'start'})")
            await vp.wait_for_timeout(2600)
            await vp.select_option("#fw-src", "leaf03.den1")
            await vp.fill("#fw-dest", "10.20.12.10")
            await vp.click("#fw-trace-go")
            await vp.wait_for_selector("#fw-trace-out .fw-layers")
            await vp.evaluate("document.getElementById('fw-trace-out').closest('.card').scrollIntoView({block: 'start'})")
            await vp.wait_for_timeout(2600)
            await vctx.close()
            make_gif(await vp.video.path(), out / "forwarding_walkthrough.gif")

    # ── Topology ────────────────────────────────────────────────────────

    @staticmethod
    async def topo_ready(p, settle=2500):
        """The device list is filled and the iframe's map has laid out."""
        await p.wait_for_selector("#tp-list .tp-row", state="attached", timeout=120000)
        await p.wait_for_function(
            "() => { const f = document.getElementById('topo-frame');"
            " const v = f && f.contentWindow && f.contentWindow.TopologyViewer;"
            " return !!(v && v.cy && v.cy.nodes().length); }", timeout=120000)
        await p.evaluate("document.fonts.ready")
        await p.wait_for_timeout(settle)

    async def topo_go(self, p, params="", settle=2500):
        # The view keeps state in the hash; a fresh load per shot keeps them independent.
        await p.goto(f"{BASE}/#/topology" + (f"?{params}" if params else ""))
        await p.reload()
        await self.topo_ready(p, settle)

    @staticmethod
    async def open_device(p, dev, tab=None):
        row = p.locator(f'#tp-list .tp-row[data-id="{dev}"]')
        await row.scroll_into_view_if_needed()
        await row.click()
        await p.wait_for_timeout(700)
        await row.dblclick()
        frame = p.frame_locator("#topo-frame")
        await frame.locator("#dm-tabs .dm-tab").first.wait_for(timeout=30000)
        if tab:
            btn = frame.locator(f'#dm-tabs .dm-tab[data-pane="{tab}"]')
            await btn.wait_for(timeout=30000)
            await btn.click()
        await p.wait_for_timeout(1200)

    async def topology(self, out: Path, then_iso: str):
        page = self.page
        await page.set_viewport_size({"width": 1440, "height": 900})

        async def wide(name):
            # Hide the device list so the map gets the width, re-fit, shoot,
            # then show the list again (the app remembers the choice).
            await page.click("#tp-toggle")
            await page.wait_for_timeout(500)
            await page.evaluate("document.getElementById('topo-frame').contentWindow.TopologyViewer.cy.fit(null, 30)")
            await page.wait_for_timeout(800)
            await self.snap(out, name)
            await page.click("#tp-toggle")

        # 1. every site, grouped
        await self.topo_go(page)
        await wide("topology_map")

        # 2. compare: two days ago against now, Denver only — the new leaf's
        #    uplinks and the re-patched leaf
        await self.topo_go(page, f"site=den1,den2&from={then_iso}", settle=3500)
        await self.snap(out, "topology_compare")
        await page.click('#tp-filters [data-tpf="changed"]')
        await page.wait_for_timeout(1200)
        await self.snap(out, "topology_compare_changed")

        # 3. link-state issues: Amsterdam's one-sided and unverified links
        await self.topo_go(page, "site=ams1", settle=3000)
        await page.click('#tp-filters [data-tpf="issues"]')
        await page.wait_for_timeout(1000)
        await self.snap(out, "topology_issues")

        # 4. device dialog: interfaces, hardware, a leaf's MAC table
        for dev, tab, name in (("spine1.den1", "interfaces", "topology_device"),
                               ("leaf03.den1", "hardware", "topology_device_hardware"),
                               ("leaf03.den1", "mactable", "topology_device_mac")):
            await self.topo_go(page, "site=den1")
            await self.open_device(page, dev, tab)
            await self.snap(out, name)

        # 5. dark theme
        await self.set_theme("dark")
        await self.topo_go(page, "site=den2")
        await wide("topology_dark")
        await self.set_theme("light")

        # 6. optional walkthrough GIF: map, compare, a device
        if self.args.gif:
            vctx, vp = await self.video_page({"width": 1180, "height": 760})
            await self.topo_go(vp, "site=den1,den2", 2500)
            await self.topo_go(vp, f"site=den1,den2&from={then_iso}", 2500)
            await vp.click('#tp-filters [data-tpf="changed"]')
            await vp.wait_for_timeout(2000)
            await vp.click('#tp-filters [data-tpf="all"]')
            await vp.wait_for_timeout(800)
            await self.topo_go(vp, "site=den1", 2000)
            await self.open_device(vp, "spine1.den1", "interfaces")
            await vp.wait_for_timeout(1800)
            await vp.frame_locator("#topo-frame").locator('#dm-tabs .dm-tab[data-pane="neighbors"]').click()
            await vp.wait_for_timeout(1800)
            await vctx.close()
            make_gif(await vp.video.path(), out / "topology_walkthrough.gif")


# The tour's caption lives in sessionStorage and is redrawn on every page load,
# so a scene that needs a reload has its caption from the first frame.
CAPTION_INIT = """
window.__tourCap = ([title, sub]) => {
    let el = document.getElementById('tour-cap');
    if (!el) {
        el = document.createElement('div');
        el.id = 'tour-cap';
        document.body.appendChild(el);
    }
    el.style.cssText = 'position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:2147483647;' +
        'background:rgba(15,23,42,.93);color:#fff;padding:11px 22px 12px;border-radius:10px;' +
        'font-family:"IBM Plex Sans",system-ui,sans-serif;box-shadow:0 8px 28px rgba(0,0,0,.28);' +
        'text-align:center;max-width:78%;pointer-events:none';
    el.innerHTML = '<div style="font-weight:600;font-size:19px">' + title + '</div>' +
        '<div style="font-weight:400;font-size:14.5px;opacity:.82;margin-top:3px">' + sub + '</div>';
};
document.addEventListener('DOMContentLoaded', () => {
    try {
        const t = sessionStorage.getItem('tour-cap');
        if (t) window.__tourCap(JSON.parse(t));
    } catch (e) { /* storage off */ }
});
"""


async def tour(s, root: Path, then_iso: str):
    """One recorded walk through the headline features: screenshots/netlapse_tour.gif."""
    vctx, p = await s.video_page({"width": 1440, "height": 810})
    await vctx.add_init_script(CAPTION_INIT)

    async def cap(title, sub):
        await p.evaluate("t => { sessionStorage.setItem('tour-cap', JSON.stringify(t)); window.__tourCap(t); }",
                         [title, sub])

    async def topo_list(shown):
        await p.evaluate("v => localStorage.setItem('netlapse.topology.panel', v)", "open" if shown else "closed")

    async def ids():
        return {d["name"]: d["id"] for d in (await (await p.request.get(f"{BASE}/api/v1/devices")).json())}

    dev = await ids()

    async def nav(hash_, ready):
        # In-app navigation: no reload, so no blank frame between scenes.
        await p.evaluate("h => { location.hash = h; }", hash_)
        await p.wait_for_selector(ready, timeout=120000)

    # 0. dashboard (the recording starts on a blank page; trimmed below)
    t0 = time.time()
    await p.goto(f"{BASE}/#/dashboard")
    await p.reload()
    await p.wait_for_selector("#btn-theme")
    await p.evaluate("document.fonts.ready")
    await p.wait_for_timeout(700)
    lead_in = time.time() - t0
    await cap("Netlapse", "Network state, collected, parsed and versioned — self-hosted, no external services")
    await p.wait_for_timeout(2800)

    # 1. topology, every site (the view needs a fresh load per hash state)
    await cap("Topology from the LLDP you already collect", "Every link graded: confirmed, one-sided, unverified — sites drawn as their own boxes")
    await topo_list(False)
    await s.topo_go(p, "", 3800)
    await topo_list(True)

    # 2. compare two days ago with now
    await cap("Compare any two points in time", "A new leaf's uplinks in green, a re-patched leaf in orange, everything untouched faded")
    await s.topo_go(p, f"site=den1,den2&from={then_iso}", 1200)
    await p.click('#tp-filters [data-tpf="changed"]')
    await p.wait_for_timeout(3200)

    # 3. device dialog
    await cap("Every capture, at the map's time", "Interfaces, neighbors, ARP, MAC table, config — each tab says which capture and when")
    await s.topo_go(p, "site=den1", 600)
    await s.open_device(p, "spine1.den1", "neighbors")
    await p.wait_for_timeout(3400)

    # 4. forwarding matrix
    await cap("Forwarding: every TOR to every TOR", "Computed from stored route tables — the red column is a subnet hidden by a Reject aggregate")
    await nav("#/forwarding?site=den1,den2", "#fw-mx td[data-k]")
    await p.wait_for_timeout(3800)

    # 5. a pair, both directions
    await p.click('#fw-mx td[data-k="leaf05.den2|leaf03.den1"]')
    await p.wait_for_selector("#fw-detail .fw-layers")
    await p.evaluate("document.getElementById('fw-detail').scrollIntoView({block: 'start'})")
    await p.evaluate("window.scrollBy(0, -70)")
    await cap("Both directions, hop by hop", "Asymmetric paths called out, with the route each hop used")
    await p.wait_for_timeout(3800)

    # 6. semantic diff: the NIC swap on leaf03.den1
    await cap("Semantic diff", "Not just changed lines: the record and field that moved — here a server's NIC was replaced")
    await nav(f"#/device/{dev['leaf03.den1']}/diff/arp", "text=MAC_ADDRESS")
    await p.wait_for_timeout(3800)

    # 7. one search
    await cap("One Search", "One token across inventory and every stored capture")
    await nav("#/onesearch", "#one-search-input")
    await p.type("#one-search-input", "198.51.100.1", delay=70)
    await p.click("#one-search-btn")
    await p.wait_for_selector("text=Routing table", timeout=30000)
    await p.wait_for_timeout(300)
    await cap("One Search", "A transit peer's IP: its BGP config, ARP entry, BGP session and routes, in one view")
    await p.wait_for_timeout(3600)

    # 8. template lab
    await cap("Template Lab", "Load any stored capture, sweep the template database, see the parsed records")
    await nav("#/templates", "#lab-load-snap")
    await p.click("#lab-load-snap")
    await p.wait_for_timeout(400)
    label = [o for o in await p.eval_on_selector_all("#snap-device option", "os => os.map(o => o.textContent)")
             if o.startswith("leaf21.den1")][0]
    await p.select_option("#snap-device", label=label)
    await p.fill("#snap-type", "lldp")
    await p.click("#snap-load")
    await p.wait_for_timeout(700)
    await p.click("#lab-test-db")
    await p.wait_for_selector("text=juniper_junos_show_lldp_neighbors", timeout=30000)
    await p.wait_for_timeout(3600)

    await vctx.close()
    make_gif(await p.video.path(), root / "netlapse_tour.gif", width=1200, start=max(0.0, lead_in - 0.2))


async def shoot(args, root: Path, then_iso: str):
    from playwright.async_api import async_playwright

    css, font_files = font_css(Path(args.fonts_dir)) if args.fonts_dir else ("", {})
    async with async_playwright() as p:
        launch = {"args": ["--no-sandbox"]}
        if args.chrome:
            launch["executable_path"] = args.chrome
        browser = await p.chromium.launch(**launch)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=args.scale)

        async def fonts(route):
            url = route.request.url
            if "fonts.googleapis.com" in url:
                await route.fulfill(status=200, content_type="text/css", body=css)
            elif url in font_files:
                await route.fulfill(status=200, content_type="font/woff2", body=font_files[url].read_bytes())
            else:
                await route.abort()

        async def use_fonts(c):
            if css:
                await c.route("https://fonts.googleapis.com/**", fonts)
                await c.route("https://fonts.gstatic.com/**", fonts)
                await c.route("https://fonts.local/**", fonts)
        await use_fonts(ctx)

        page = await ctx.new_page()
        await page.request.post(f"{BASE}/api/v1/auth/login", data={"username": "admin", "password": PASSWORD})
        await page.request.post(f"{BASE}/api/v1/setup/complete")

        s = Shooter(args, browser, ctx, page, use_fonts, root / "_video")
        await s.set_theme("light")
        try:
            for section in ("forwarding", "topology"):
                if args.only in ("all", section):
                    out = root / section
                    out.mkdir(parents=True, exist_ok=True)
                    await getattr(s, section)(out, then_iso)
            if args.only == "tour" or (args.only == "all" and args.gif):
                await tour(s, root, then_iso)
        finally:
            await ctx.close()
            await browser.close()
            shutil.rmtree(s.tmp_video, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Regenerate Forwarding and Topology screenshots from the demo lab")
    ap.add_argument("--out", default=str(ROOT / "screenshots"),
                    help="root folder; forwarding/ and topology/ go under it")
    ap.add_argument("--only", choices=("all", "forwarding", "topology", "tour"), default="all",
                    help="tour: just the feature-tour GIF (screenshots/netlapse_tour.gif)")
    ap.add_argument("--demo-dir", default=None, help="where to build the demo lab (default: a temp dir)")
    ap.add_argument("--chrome", default=None, help="Chrome/Chromium executable (default: Playwright's)")
    ap.add_argument("--fonts-dir", default=None, help="fontsource folder with ibm-plex-sans/ and ibm-plex-mono/")
    ap.add_argument("--scale", type=float, default=1.5, help="device scale factor (default 1.5)")
    ap.add_argument("--gif", action="store_true", help="also record walkthrough GIFs (needs ffmpeg)")
    args = ap.parse_args()

    from tools.demo_lab import seed

    demo = Path(args.demo_dir or tempfile.mkdtemp(prefix="netlapse-demo-")).resolve()
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    seed(demo, now)
    then_iso = (now - timedelta(days=2) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ, NETLAPSE_CONFIG=str(demo / "config.yaml"), NETLAPSE_ADMIN_PASSWORD=PASSWORD,
               NETLAPSE_SKIP_TEMPLATE_DOWNLOAD="1", PYTHONPATH=str(ROOT))
    log = open(demo / "server.log", "w")
    server = subprocess.Popen([sys.executable, "-m", "netlapse", "serve"], cwd=str(demo), env=env,
                              stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_up()
        print(f"demo lab in {demo}; writing to {root}")
        asyncio.run(shoot(args, root, then_iso))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()


if __name__ == "__main__":
    main()
