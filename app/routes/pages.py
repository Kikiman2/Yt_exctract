"""HTML pages. They render a shell; the page's JavaScript loads data from /api
so every view has the same loading and error handling."""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth, config

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

public_router = APIRouter()
router = APIRouter(dependencies=[Depends(auth.require_login)])


def render(request: Request, name: str, active: str, **ctx):
    return templates.TemplateResponse(
        request,
        name,
        {"active": active, "user": auth.current_user(request), "dev_fake": config.DEV_FAKE, **ctx},
    )


# --- login / logout -------------------------------------------------------------


@public_router.get("/login")
async def login_page(request: Request, next: str | None = None):
    target = auth.safe_next(next)
    if auth.current_user(request):
        return RedirectResponse(target, status_code=303)
    return render(request, "login.html", "login", next=target, error=None)


@public_router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    target = auth.safe_next(next)
    ip = auth.client_ip(request)
    if auth.rate_limiter.blocked(ip):
        wait = auth.rate_limiter.retry_after(ip)
        resp = render(request, "login.html", "login", next=target,
                      error=f"Too many failed attempts. Try again in {wait} seconds.")
        resp.status_code = 429
        resp.headers["Retry-After"] = str(wait)
        return resp
    if not auth.check_credentials(username, password):
        auth.rate_limiter.fail(ip)
        resp = render(request, "login.html", "login", next=target, error="Wrong username or password.")
        resp.status_code = 401
        return resp
    auth.rate_limiter.reset(ip)
    auth.login_session(request, username)
    return RedirectResponse(target, status_code=303)


@public_router.post("/logout")
async def logout(request: Request):
    auth.logout_session(request)
    return RedirectResponse("/login", status_code=303)


# --- pages ------------------------------------------------------------------------


@router.get("/")
async def dashboard(request: Request):
    return render(request, "dashboard.html", "dashboard")


@router.get("/sources")
async def sources_page(request: Request):
    return render(request, "sources.html", "sources")


@router.get("/settings")
async def settings_page(request: Request):
    return render(request, "settings.html", "settings")


@router.get("/health")
async def health_page(request: Request):
    return render(request, "health.html", "health")
