# services/shodan_lookup.py
"""Shodan host lookup — passive OSINT on a single IP or domain.

Queries Shodan's public index (their data, already collected) for what's exposed
on a host: open ports, running services/banners, the org/ISP/ASN, geolocation,
and any known CVEs. This is a lookup, NOT a scan — nothing is sent to the target.
Intended for investigating a host's exposure (e.g. auditing your own servers).

Needs SHODAN_API_KEY in the environment (free tier at shodan.io).
"""
import os
import socket
import ipaddress
import logging

import httpx

logger = logging.getLogger(__name__)

_HOST_URL = "https://api.shodan.io/shodan/host/{ip}"


def _key() -> str:
    return os.environ.get("SHODAN_API_KEY", "")


def is_configured() -> bool:
    return bool(_key())


def _resolve(target: str):
    """Return an IP for `target`. If it's already an IP, pass it through; if a
    domain, resolve it locally (no Shodan credit spent)."""
    try:
        ipaddress.ip_address(target)
        return target
    except ValueError:
        pass
    try:
        return socket.gethostbyname(target)
    except Exception:
        return None


async def host_lookup(target: str) -> dict:
    if not _key():
        return {"error": "Shodan isn't set up yet — add SHODAN_API_KEY to enable it."}
    target = (target or "").strip()
    # tolerate a pasted URL
    target = target.replace("https://", "").replace("http://", "").split("/")[0].strip()
    if not target:
        return {"error": "Give me an IP or domain to look up."}

    ip = _resolve(target)
    if not ip:
        return {"error": f"Couldn't resolve '{target}' to an IP address."}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_HOST_URL.format(ip=ip), params={"key": _key()})
        if resp.status_code == 404:
            return {"error": f"Shodan has no records for {ip} (not in its index)."}
        if resp.status_code == 401:
            return {"error": "Shodan rejected the API key — check SHODAN_API_KEY."}
        resp.raise_for_status()
        d = resp.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"Shodan HTTP error: {e.response.status_code}")
        return {"error": f"Shodan returned status {e.response.status_code}."}
    except httpx.RequestError as e:
        logger.error(f"Shodan request error: {e}")
        return {"error": "Shodan is unreachable right now."}

    services = []
    for s in (d.get("data") or [])[:25]:
        services.append({
            "port": s.get("port"),
            "transport": s.get("transport", "tcp"),
            "product": (s.get("product") or "").strip(),
            "version": (s.get("version") or "").strip(),
        })

    return {
        "ip": d.get("ip_str", ip),
        "target": target,
        "org": d.get("org") or "",
        "isp": d.get("isp") or "",
        "asn": d.get("asn") or "",
        "os": d.get("os") or "",
        "country": d.get("country_name") or "",
        "city": d.get("city") or "",
        "lat": d.get("latitude"),
        "lon": d.get("longitude"),
        "hostnames": d.get("hostnames") or [],
        "ports": sorted([p for p in (d.get("ports") or []) if isinstance(p, int)]),
        "services": services,
        "vulns": sorted(d.get("vulns") or [])[:40],
        "last_update": d.get("last_update", ""),
    }


def format_for_model(d: dict) -> str:
    """Flatten a lookup into text Grace can present."""
    if "error" in d:
        return d["error"]
    lines = [f"Shodan host report for {d['target']} ({d['ip']}):"]
    loc = ", ".join(x for x in [d.get("city"), d.get("country")] if x)
    if d.get("org") or loc:
        lines.append(f"- Org/ISP: {d.get('org') or d.get('isp') or '?'}"
                     + (f" · ASN {d['asn']}" if d.get("asn") else "")
                     + (f" · {loc}" if loc else ""))
    if d.get("os"):
        lines.append(f"- OS: {d['os']}")
    if d.get("hostnames"):
        lines.append("- Hostnames: " + ", ".join(d["hostnames"][:6]))
    if d.get("ports"):
        lines.append("- Open ports: " + ", ".join(str(p) for p in d["ports"]))
    if d.get("services"):
        svc = []
        for s in d["services"]:
            label = f"{s['port']}"
            if s["product"]:
                label += f" {s['product']}"
                if s["version"]:
                    label += f" {s['version']}"
            svc.append(label)
        lines.append("- Services: " + "; ".join(svc))
    if d.get("vulns"):
        lines.append(f"- KNOWN CVEs ({len(d['vulns'])}): " + ", ".join(d["vulns"][:15])
                     + (" …" if len(d["vulns"]) > 15 else ""))
    else:
        lines.append("- No known CVEs flagged by Shodan.")
    if d.get("last_update"):
        lines.append(f"- Last seen by Shodan: {d['last_update']}")
    return "\n".join(lines)
