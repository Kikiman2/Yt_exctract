from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from .. import db

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/")
async def dashboard(request: Request):
    videos = await db.list_videos()
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "videos": videos}
    )


@router.get("/channels")
async def channels_page(request: Request):
    channels = await db.list_channels()
    return templates.TemplateResponse(
        "channels.html", {"request": request, "channels": channels}
    )


@router.get("/settings")
async def settings_page(request: Request):
    settings = await db.get_settings()
    return templates.TemplateResponse(
        "settings.html", {"request": request, "settings": settings}
    )
