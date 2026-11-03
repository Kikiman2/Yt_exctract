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
admin_router = APIRouter(dependencies=[Depends(auth.require_admin)])


def render(request: Request, name: str, active: str, **ctx):
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(
        request,
        name,
        {"active": active, "user": user, "is_admin": bool(user and user["role"] == "admin"),
         "dev_fake": config.DEV_FAKE, **ctx},
    )


# --- login / logout -------------------------------------------------------------


@public_router.get("/login")
async def login_page(request: Request, next: str | None = None):
    target = auth.safe_next(next)
    if await auth.load_user(request):
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
    user = await auth.authenticate(username, password)
    if user is None:
        auth.rate_limiter.fail(ip)
        resp = render(request, "login.html", "login", next=target, error="Wrong username or password.")
        resp.status_code = 401
        return resp
    auth.rate_limiter.reset(ip)
    auth.login_session(request, user)
    return RedirectResponse(target, status_code=303)


@public_router.post("/logout")
async def logout(request: Request):
    auth.logout_session(request)
    return RedirectResponse("/login", status_code=303)


# --- pages ------------------------------------------------------------------------


@router.get("/")
async def dashboard(request: Request, user: dict = Depends(auth.require_login)):
    if user["role"] != "admin":
        return render(request, "my_downloads.html", "dashboard")
    return render(request, "dashboard.html", "dashboard")


@router.get("/account")
async def account_page(request: Request):
    return render(request, "account.html", "account")


@admin_router.get("/sources")
async def sources_page(request: Request):
    return render(request, "sources.html", "sources")


@admin_router.get("/settings")
async def settings_page(request: Request):
    return render(request, "settings.html", "settings")


@admin_router.get("/health")
async def health_page(request: Request):
    return render(request, "health.html", "health")


@admin_router.get("/users")
async def users_page(request: Request):
    return render(request, "users.html", "users")
