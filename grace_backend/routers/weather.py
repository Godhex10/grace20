# routers/weather.py
from fastapi import APIRouter, Query
from typing import Optional

from services import weather as weather_service

router = APIRouter(prefix="/api/weather", tags=["Weather"])


@router.get("/")
async def current_weather(location: Optional[str] = Query(default=None)):
    """Live current conditions + short forecast for the configured (or given)
    location. Returns the weather payload, or {'error': ...} on failure."""
    return await weather_service.get_weather(location)
