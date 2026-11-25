#!/usr/bin/env python3
"""
screenshots.py — regenerate the Forwarding documentation screenshots.

Builds the synthetic demo network (tools/demo_lab.py), starts netlapse on
it, drives the web UI in headless Chromium and writes PNGs (and optionally
an animated GIF) to screenshots/forwarding/. Nothing real is shown: every
hostname and address comes from the demo lab.

Needs Playwright (pip install playwright; playwright install chromium) and,
for --gif, ffmpeg. Where Google Fonts can't be reached (sandboxes, CI),
point --fonts-dir at a folder holding the IBM Plex Sans/Mono latin woff2
files (npm i @fontsource/ibm-plex-sans @fontsource/ibm-plex-mono gives them)
and the page gets them locally instead of falling back to system fonts.

    python tools/screenshots.py
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
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSWORD = "demo-pass-1"
PORT = 8898
BASE = f"http://127.0.0.1:{PORT}"


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


async def shoot(args, out: Path, then_iso: str):
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
        api = page.request
        await api.post(f"{BASE}/api/v1/auth/login", data={"username": "admin", "password": PASSWORD})
        await api.post(f"{BASE}/api/v1/setup/complete")

        async def set_theme(theme):
            # Through the app's own toggle: it stores the preference and reloads.
            await page.goto(f"{BASE}/#/forwarding")
            await page.wait_for_selector("#btn-theme")
            is_dark = "dark" in (await page.get_attribute("#theme-css", "href") or "")
            if is_dark != (theme == "dark"):
                async with page.expect_navigation():
                    await page.click("#btn-theme")
                await page.wait_for_selector("#btn-theme")

        await set_theme("light")

        async def go(hash_, ready="#fw-mx td[data-k]"):
            await page.goto(f"{BASE}/{hash_}")
            await page.wait_for_selector(ready, timeout=120000)
            await page.evaluate("document.fonts.ready")
            await page.wait_for_timeout(600)

        def card(sel):
            return page.locator(sel).locator("xpath=ancestor-or-self::div[contains(@class,'card')][1]")

        async def snap(name, locator=None, full=False):
            path = out / f"{name}.png"
            if locator is not None:
                await locator.scroll_into_view_if_needed()
                await locator.screenshot(path=str(path))
            else:
                await page.screenshot(path=str(path), full_page=full)
            print(f"  {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")

        # 1. overview: controls, cards, matrix
        await page.set_viewport_size({"width": 1440, "height": 1180})
        await go("#/forwarding")
        await snap("forwarding_overview")

        # 2. a cross-site pair, both directions (tall viewport: the card must fit
        #    under the fixed page header or the header lands in the shot)
        await page.set_viewport_size({"width": 1440, "height": 2000})
        await go("#/forwarding?pair=leaf05.den2|leaf03.den1", "#fw-detail .fw-layers")
        await snap("forwarding_pair", card("#fw-detail"))

        # 3. first-hop fanout: the single-homed leafs light up
        await page.set_viewport_size({"width": 1440, "height": 1200})
        await go("#/forwarding?color=fanout")
        await snap("forwarding_fanout", card("#fw-mx"))

        # 4. trace into the black hole
        await go("#/forwarding?trace=leaf03.den1~10.20.12.10", "#fw-trace-out .fw-layers")
        await snap("forwarding_trace_blackhole", card("#fw-trace-out"))

        # 5. trace the long way round: spine2.den1 to a leaf it lost its port to
        await go("#/forwarding?trace=spine2.den1~leaf07.den1", "#fw-trace-out .fw-layers")
        await snap("forwarding_trace_long_way", card("#fw-trace-out"))

        # 6. data quality and the per-endpoint summary
        await go("#/forwarding")
        await snap("forwarding_quality", card("#fw-quality"))
        await snap("forwarding_summary", card("#fw-sum"))

        # 7. two days ago: leaf07/08 still dual-homed
        await go(f"#/forwarding?as_of={then_iso}&color=fanout")
        await snap("forwarding_fanout_as_of", card("#fw-mx"))

        # 8. dark theme
        await set_theme("dark")
        await page.set_viewport_size({"width": 1440, "height": 1180})
        await go("#/forwarding?pair=leaf03.den1|leaf07.den1")
        await snap("forwarding_dark")
        await set_theme("light")

        # 9. standalone HTML report
        await page.set_viewport_size({"width": 1440, "height": 1000})
        await page.goto(f"{BASE}/api/v1/forwarding/report")
        await page.wait_for_selector("#mx td[data-k]")
        await page.evaluate("document.fonts.ready")
        await page.wait_for_timeout(400)
        await snap("forwarding_report")

        # 10. optional walkthrough GIF, in its own context so the video holds
        #     only the walkthrough, at a size whose text survives downscaling
        video_path = None
        if args.gif:
            size = {"width": 1180, "height": 760}
            vctx = await browser.new_context(viewport=size, record_video_dir=str(out / "_video"),
                                              record_video_size=size)
            await use_fonts(vctx)
            await vctx.add_cookies(await ctx.cookies())
            vp = await vctx.new_page()
            await vp.goto(f"{BASE}/#/forwarding")
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
            video_path = await vp.video.path()

        await ctx.close()
        await browser.close()

    if video_path and shutil.which("ffmpeg"):
        gif = out / "forwarding_walkthrough.gif"
        palette = out / "_palette.png"
        # Small enough to live in the repo: 4 fps, 1000 wide, 64-color palette.
        vf = "fps=4,scale=1000:-1:flags=lanczos"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-vf",
                        f"{vf},palettegen=max_colors=64:stats_mode=diff", str(palette)], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-i", str(palette), "-lavfi",
                        f"{vf} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
                        str(gif)], check=True)
        palette.unlink(missing_ok=True)
        print(f"  {gif.relative_to(ROOT)}")
    shutil.rmtree(out / "_video", ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Regenerate Forwarding screenshots from the demo lab")
    ap.add_argument("--out", default=str(ROOT / "screenshots" / "forwarding"))
    ap.add_argument("--demo-dir", default=None, help="where to build the demo lab (default: a temp dir)")
    ap.add_argument("--chrome", default=None, help="Chrome/Chromium executable (default: Playwright's)")
    ap.add_argument("--fonts-dir", default=None, help="fontsource folder with ibm-plex-sans/ and ibm-plex-mono/")
    ap.add_argument("--scale", type=float, default=1.5, help="device scale factor (default 1.5)")
    ap.add_argument("--gif", action="store_true", help="also record a walkthrough GIF (needs ffmpeg)")
    args = ap.parse_args()

    from tools.demo_lab import seed

    demo = Path(args.demo_dir or tempfile.mkdtemp(prefix="netlapse-demo-")).resolve()
    now = datetime.utcnow().replace(microsecond=0)
    seed(demo, now)
    then_iso = (now - timedelta(days=2) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ, NETLAPSE_CONFIG=str(demo / "config.yaml"), NETLAPSE_ADMIN_PASSWORD=PASSWORD,
               NETLAPSE_SKIP_TEMPLATE_DOWNLOAD="1", PYTHONPATH=str(ROOT))
    log = open(demo / "server.log", "w")
    server = subprocess.Popen([sys.executable, "-m", "netlapse", "serve"], cwd=str(demo), env=env,
                              stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_up()
        print(f"demo lab in {demo}; writing to {out}")
        asyncio.run(shoot(args, out, then_iso))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()


if __name__ == "__main__":
    main()
