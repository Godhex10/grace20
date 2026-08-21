# routers/map_proxy.py
import os
import httpx
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/map", tags=["Map Proxy"])

TOMTOM_API_KEY = os.environ.get("TOMTOM_API_KEY")
TOMTOM_BASE_URL = "https://api.tomtom.com/traffic/map/4/tile/flow/relative0"


@router.get("/traffic/{z}/{x}/{y}.png")
async def proxy_traffic_tile(z: int, x: int, y: int):
    if not TOMTOM_API_KEY:
        raise HTTPException(status_code=503, detail="TomTom API key not configured")
    
    if not (0 <= z <= 18):
        raise HTTPException(status_code=400, detail="Invalid zoom level")
    
    max_tile = 2 ** z
    if not (0 <= x < max_tile) or not (0 <= y < max_tile):
        raise HTTPException(status_code=400, detail="Invalid tile coordinates")
    
    url = f"{TOMTOM_BASE_URL}/{z}/{x}/{y}.png"
    params = {"key": TOMTOM_API_KEY}
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return Response(content=resp.content, media_type="image/png")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Tile service error")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail="Tile service unavailable")