# services/directions.py
"""Traffic-aware directions via TomTom — geocode two places, route between them
with live traffic, and return distance + ETA + delay + the route geometry to
draw on the map. Needs TOMTOM_API_KEY (free tier at developer.tomtom.com).
"""
import os
import logging
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Fuzzy Search (not plain geocode) so landmarks/POIs like "Lekki Conservation
# Centre" resolve, not just street addresses.
_GEOCODE = "https://api.tomtom.com/search/2/search/{q}.json"
_ROUTE = "https://api.tomtom.com/routing/1/calculateRoute/{a}:{b}/json"


def _key() -> str:
    return os.environ.get("TOMTOM_API_KEY", "")


def is_configured() -> bool:
    return bool(_key())


async def _geocode(client, place: str, near=None, country=None):
    """Place name → {name, lat, lon, cc} or None. `near`=(lat,lon) biases results
    toward that point and `country`=ISO code constrains them to that country, so
    ambiguous names (e.g. 'conservation center') resolve to the one near the other
    endpoint — not a namesake on another continent."""
    place = (place or "").strip()
    if not place:
        return None
    params = {"key": _key(), "limit": 1}
    if near and near[0] is not None and near[1] is not None:
        params["lat"] = near[0]
        params["lon"] = near[1]
    if country:
        params["countrySet"] = country
    try:
        resp = await client.get(_GEOCODE.format(q=quote(place)), params=params)
        resp.raise_for_status()
        results = (resp.json() or {}).get("results") or []
    except Exception as e:
        logger.warning(f"TomTom geocode failed for '{place}': {e}")
        return None
    if not results:
        return None
    r = results[0]
    pos = r.get("position") or {}
    addr = r.get("address") or {}
    return {
        "name": addr.get("freeformAddress", place),
        "lat": pos.get("lat"),
        "lon": pos.get("lon"),
        "cc": addr.get("countryCode", ""),
    }


def _fmt_minutes(seconds):
    m = int(round((seconds or 0) / 60))
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    return f"{h} hr {mm} min" if mm else f"{h} hr"


def _route_points(rt):
    pts = []
    for leg in rt.get("legs") or []:
        for p in leg.get("points") or []:
            if p.get("latitude") is not None:
                pts.append([p["latitude"], p["longitude"]])
    return pts


def _traffic_segments(rt, points):
    """Slice out the stretches of the route that TomTom flagged as congested, so
    the frontend can paint them orange/red ON the route line. `sections` reference
    indices into the route's flattened points (aligned with `_route_points`)."""
    segs = []
    for s in rt.get("sections") or []:
        if (s.get("sectionType") or "").upper() != "TRAFFIC":
            continue
        i0, i1 = s.get("startPointIndex"), s.get("endPointIndex")
        if i0 is None or i1 is None:
            continue
        sub = points[i0:i1 + 1]
        if len(sub) < 2:
            continue
        mag = s.get("magnitudeOfDelay") or 0  # 0 unknown,1 minor,2 moderate,3 major,4 closure
        segs.append({"level": "heavy" if mag >= 3 else "moderate", "points": sub})
    return segs


async def get_route(origin: str, destination: str, arrive_by: str = "") -> dict:
    if not _key():
        return {"error": "Directions aren't set up — add TOMTOM_API_KEY to enable them."}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            a = await _geocode(client, origin)
            if not a or a["lat"] is None:
                return {"error": f"Couldn't find '{origin}' on the map."}
            # Constrain the destination to the origin's country (+ proximity bias)
            # so a namesake elsewhere doesn't win — this is what sent 'Lekki
            # conservation center' to Arizona and broke routing. Driving routes are
            # almost always within one country, so this is safe and still allows
            # long in-country trips (e.g. Lagos → Abuja).
            b = await _geocode(client, destination, near=(a["lat"], a["lon"]), country=a.get("cc") or None)
            if not b or b["lat"] is None:
                return {"error": f"Couldn't find '{destination}' on the map."}

            url = _ROUTE.format(a=f"{a['lat']},{a['lon']}", b=f"{b['lat']},{b['lon']}")
            resp = await client.get(url, params={
                "key": _key(),
                "traffic": "true",
                "travelMode": "car",
                "computeTravelTimeFor": "all",
                "routeType": "fastest",
                "maxAlternatives": 2,
                "sectionType": "traffic",  # returns congested stretches to paint on the route
            })
            if resp.status_code == 403:
                return {"error": "TomTom rejected the routing request — check the API key/plan."}
            if resp.status_code == 400:
                detail = ""
                try:
                    detail = (resp.json().get("detailedError") or {}).get("message", "")
                except Exception:
                    pass
                if "NO_ROUTE" in detail.upper() or "PRODUCTID" in detail.upper():
                    return {"error": (f"No drivable route between '{a['name']}' and "
                                      f"'{b['name']}' — that destination probably matched "
                                      "the wrong place. Try a more specific name (add the city).")}
                return {"error": "Couldn't work out that route — try more specific place names."}
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"Routing service returned status {e.response.status_code}."}
    except httpx.RequestError:
        return {"error": "Routing service is unreachable right now."}

    routes = data.get("routes") or []
    if not routes:
        return {"error": f"No drivable route found from {origin} to {destination}."}

    primary = routes[0].get("summary") or {}
    alternatives = []
    for rt in routes[1:]:
        s = rt.get("summary") or {}
        alternatives.append({
            "distance_km": round((s.get("lengthInMeters") or 0) / 1000, 1),
            "time_s": s.get("travelTimeInSeconds"),
            "delay_s": s.get("trafficDelayInSeconds") or 0,
            "points": _route_points(rt),
        })

    out = {
        "origin": a,
        "destination": b,
        "distance_km": round((primary.get("lengthInMeters") or 0) / 1000, 1),
        "time_s": primary.get("travelTimeInSeconds"),
        "no_traffic_s": primary.get("noTrafficTravelTimeInSeconds"),
        "delay_s": primary.get("trafficDelayInSeconds") or 0,
        "points": _route_points(routes[0]),
        "traffic_segments": _traffic_segments(routes[0], _route_points(routes[0])),
        "alternatives": alternatives,
    }

    # Arrival-time planning: "leave by X to arrive by Y".
    if arrive_by:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                target = datetime.strptime(arrive_by.strip(), fmt)
                out["arrive_by"] = target
                out["leave_by"] = target - timedelta(seconds=out["time_s"] or 0)
                break
            except ValueError:
                continue
    return out


def route_summary(d: dict) -> dict:
    """Structured summary for the on-map directions card."""
    delay = d.get("delay_s") or 0
    level = "heavy" if delay >= 600 else ("moderate" if delay >= 60 else "light")
    return {
        "origin": d["origin"]["name"],
        "destination": d["destination"]["name"],
        "eta": _fmt_minutes(d.get("time_s")),
        "distance_km": d.get("distance_km"),
        "delay": (f"+{_fmt_minutes(delay)} vs clear roads" if delay >= 60 else "Light traffic"),
        "delay_level": level,
        "leave_by": d["leave_by"].strftime("%I:%M %p").lstrip("0") if d.get("leave_by") else None,
        "arrive_by": d["arrive_by"].strftime("%I:%M %p").lstrip("0") if d.get("arrive_by") else None,
        "alternatives": [
            {"eta": _fmt_minutes(a.get("time_s")), "distance_km": a.get("distance_km")}
            for a in d.get("alternatives", [])
        ],
    }


def format_for_model(d: dict) -> str:
    if "error" in d:
        return d["error"]
    delay = d.get("delay_s") or 0
    lines = [
        f"Route: {d['origin']['name']} → {d['destination']['name']}",
        f"- Distance: {d['distance_km']} km",
        f"- Travel time now (with traffic): {_fmt_minutes(d['time_s'])}",
    ]
    if delay >= 60:
        lines.append(f"- Traffic delay: about {_fmt_minutes(delay)} slower than clear roads"
                     f" ({_fmt_minutes(d.get('no_traffic_s'))} with no traffic)")
    else:
        lines.append("- Traffic: light — roads are basically clear right now")

    # Arrival-time planning.
    if d.get("leave_by") and d.get("arrive_by"):
        lines.append(f"- To arrive by {d['arrive_by'].strftime('%I:%M %p').lstrip('0')}, "
                     f"leave by {d['leave_by'].strftime('%I:%M %p').lstrip('0')}.")

    # Alternative routes.
    alts = d.get("alternatives") or []
    if alts:
        parts = [f"{_fmt_minutes(a['time_s'])} ({a['distance_km']} km)" for a in alts]
        lines.append(f"- {len(alts)} alternative route(s): " + "; ".join(parts)
                     + " — shown dimmer on the map.")
    lines.append("- The fastest route is drawn on the tactical map.")
    return "\n".join(lines)
