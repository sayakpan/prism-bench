"""
GET /api/health

Reports which AI providers are configured. Returns 200 as long as the
service is up â€” missing provider keys are not a health failure.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from moodboard_ai.config import get_settings

router = APIRouter()


def _iso_z(dt: datetime | None = None) -> str:
    """JS-style ISO timestamp: 2025-05-30T12:34:56.789Z."""
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@router.get("/")
async def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "timestamp": _iso_z(),
        "providers": {
            "anthropic": bool(settings.anthropic_api_key),
            "openai": bool(settings.openai_api_key),
            "gemini": bool(settings.gemini_api_key),
        },
    }
