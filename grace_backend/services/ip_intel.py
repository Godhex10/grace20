# services/ip_intel.py
"""Free IP/domain geolocation + hosting intel (no key, no paywall).

Resolves a domain to its IP and looks up where it's hosted (country, city,
coords), the hosting org / ISP / ASN, and reverse DNS — via ip-api.com's free
endpoint. This is the always-works base layer; Shodan (paid) enriches it with
open ports and CVEs when available.
"""
import socket
import ipaddress
import logging

import httpx

logger = logging.getLogger(__name__)

_URL = "http://ip-api.com/json/{ip}"
_FIELDS = "status,message,country,countryCode,regionName,city,lat,lon,isp,org,as,asname,reverse,query"


def resolve(target: str):
    """Domain or IP → IP string (or None). No credits, local DNS."""
    target = (target or "").strip()
    target = target.replace("https://", "").replace("http://", "").split("/")[0].strip()
    if not target:
        return None, None
    try:
        ipaddress.ip_address(target)
        return target, target
    except ValueError:
        pass
    try:
        return socket.gethostbyname(target), target
    except Exception:
        return None, target


async def geo_lookup(target: str) -> dict:
    ip, cleaned = resolve(target)
    if not ip:
        return {"error": f"Couldn't resolve '{cleaned or target}' to an IP address."}
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(_URL.format(ip=ip), params={"fields": _FIELDS})
            resp.raise_for_status()
            d = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"Geo service returned status {e.response.status_code}."}
    except httpx.RequestError:
        return {"error": "Geolocation service is unreachable right now."}

    if d.get("status") != "success":
        return {"error": d.get("message", "Geolocation lookup failed.")}

    return {
        "ip": d.get("query", ip),
        "target": cleaned,
        "country": d.get("country") or "",
        "region": d.get("regionName") or "",
        "city": d.get("city") or "",
        "lat": d.get("lat"),
        "lon": d.get("lon"),
        "isp": d.get("isp") or "",
        "org": d.get("org") or "",
        "asn": d.get("as") or "",
        "asname": d.get("asname") or "",
        "hostnames": [d["reverse"]] if d.get("reverse") else [],
    }


def format_for_model(d: dict) -> str:
    if "error" in d:
        return d["error"]
    loc = ", ".join(x for x in [d.get("city"), d.get("region"), d.get("country")] if x)
    lines = [f"Hosting/location for {d['target']} ({d['ip']}):"]
    if loc:
        lines.append(f"- Hosted in: {loc}")
    if d.get("org") or d.get("isp"):
        who = d.get("org") or d.get("isp")
        lines.append(f"- Host/ISP: {who}" + (f" (ISP: {d['isp']})" if d.get("isp") and d.get("org") and d["isp"] != d["org"] else ""))
    if d.get("asn"):
        lines.append(f"- Network: {d['asn']}" + (f" — {d['asname']}" if d.get("asname") else ""))
    if d.get("hostnames"):
        lines.append(f"- Reverse DNS: {', '.join(d['hostnames'])}")
    # Flag likely CDN so he understands "where hosted" is the edge, not origin.
    blob = f"{d.get('org','')} {d.get('isp','')} {d.get('asname','')}".lower()
    if any(c in blob for c in ("cloudflare", "akamai", "fastly", "amazon", "google", "vercel", "netlify", "cloudfront")):
        lines.append("- Note: this looks like a CDN/cloud edge — it's the front, not necessarily the origin server.")
    return "\n".join(lines)
