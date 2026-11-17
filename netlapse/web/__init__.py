"""
Netlapse Web UI Router

Serves the single-page application shell and static assets.
The frontend is vanilla JS with ES modules — no build step, no bundler.

Mount in app.py:
    from netlapse.web import router as web_router, STATIC_DIR
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter(tags=["web"])

STATIC_DIR = Path(__file__).parent / "static"

_VALID_THEMES = {"light", "dark"}


@router.get("/", include_in_schema=False)
async def index(request: Request):
    """Serve the SPA shell, with the theme stylesheet chosen from the
    logged-in user's saved preference. The correct CSS is linked before
    first paint, so there is no flash-of-wrong-theme on load."""
    user = getattr(request.state, "user", None) or {}
    theme = user.get("theme") or "light"
    if theme not in _VALID_THEMES:
        theme = "light"
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    if theme == "dark":
        html = html.replace("netlapse.css", "netlapse-dark.css", 1)
    if theme == "light":
        html = html.replace("netlapse.css", "netlapse-light.css", 1)

    return HTMLResponse(html)