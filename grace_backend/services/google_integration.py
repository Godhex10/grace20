# services/google_integration.py
"""Gmail + Google Calendar for Grace (read-only).

Uses the refresh token saved by connect_google.py (google_token.json). Every call
loads the token, refreshes the short-lived access token if needed (persisting the
refresh), and talks to the Google APIs. Degrades gracefully when not connected.
"""
import os
import re
import base64
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # grace_backend


def _token_path() -> str:
    """Prefer a token in the per-user config dir (packaged app) if present,
    otherwise the one beside the code (dev)."""
    try:
        from services import appconfig
        cfg = os.path.join(appconfig.config_dir(), "google_token.json")
        if os.path.exists(cfg):
            return cfg
    except Exception:
        pass
    return os.path.join(_HERE, "google_token.json")


TOKEN_PATH = _token_path()
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",   # read + archive/mark-read/label/trash
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/contacts.readonly",        # look up people by name
    "https://www.googleapis.com/auth/contacts.other.readonly",  # + people he's emailed before
]


def is_connected() -> bool:
    return os.path.exists(TOKEN_PATH)


def _creds():
    if not os.path.exists(TOKEN_PATH):
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        # scopes=None → adopt whatever the saved token was actually granted, so
        # adding new scopes (e.g. contacts) never breaks refresh for a token that
        # predates them. New scopes simply take effect after the next re-consent.
        creds = Credentials.from_authorized_user_file(TOKEN_PATH)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        return creds
    except Exception as e:
        logger.warning(f"[google] credential load/refresh failed: {e}")
        return None


def _service(name, version):
    creds = _creds()
    if not creds:
        return None
    try:
        from googleapiclient.discovery import build
        return build(name, version, credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.warning(f"[google] build {name} failed: {e}")
        return None


# ── Gmail ───────────────────────────────────────────────────────────────────
def _headers(msg) -> dict:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _short_from(raw: str) -> str:
    """'Zenith Bank <no-reply@z.com>' -> 'Zenith Bank'."""
    raw = (raw or "").strip()
    m = re.match(r'^"?([^"<]+?)"?\s*<', raw)
    return (m.group(1).strip() if m else raw).strip()


def _email_addr(raw: str) -> str:
    """'Zenith Bank <no-reply@z.com>' -> 'no-reply@z.com' (for replying)."""
    m = re.search(r"<([^>]+)>", raw or "")
    if m:
        return m.group(1).strip()
    raw = (raw or "").strip()
    return raw if "@" in raw else ""


def _estimate(query: str) -> int:
    g = _service("gmail", "v1")
    if not g:
        return -1
    try:
        return int(g.users().messages().list(userId="me", q=query, maxResults=1)
                   .execute().get("resultSizeEstimate", 0))
    except Exception:
        return -1


def _list(query: str, max_results: int):
    g = _service("gmail", "v1")
    if not g:
        return None
    res = g.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    msgs = res.get("messages", [])
    if not msgs:
        return []
    # Fetch every message's metadata in ONE batched HTTP request instead of N
    # sequential round-trips (each was ~150ms to Google — the main slow bit).
    out = [None] * len(msgs)

    def _cb(request_id, response, exception):
        if exception or not response:
            return
        i = int(request_id)
        h = _headers(response)
        out[i] = {
            "id": response.get("id"),
            "from": _short_from(h.get("from", "")),
            "subject": h.get("subject", "(no subject)"),
            "date": h.get("date", ""),
            "snippet": (response.get("snippet") or "").strip(),
        }

    try:
        batch = g.new_batch_http_request()
        for i, m in enumerate(msgs):
            batch.add(
                g.users().messages().get(userId="me", id=m["id"], format="metadata",
                                         metadataHeaders=["From", "Subject", "Date"]),
                request_id=str(i), callback=_cb)
        batch.execute()
        return [x for x in out if x]
    except Exception as e:
        logger.warning(f"[google] batch metadata fetch failed, falling back: {e}")
        out2 = []
        for m in msgs:
            try:
                d = g.users().messages().get(userId="me", id=m["id"], format="metadata",
                                             metadataHeaders=["From", "Subject", "Date"]).execute()
                h = _headers(d)
                out2.append({"id": m["id"], "from": _short_from(h.get("from", "")),
                             "subject": h.get("subject", "(no subject)"), "date": h.get("date", ""),
                             "snippet": (d.get("snippet") or "").strip()})
            except Exception:
                pass
        return out2


def unread_emails(max_results=8):
    """Most recent unread (newest first) + how many arrived in the last day —
    a useful 'what's new' rather than the overwhelming total-unread backlog."""
    if not is_connected():
        return {"error": "Google isn't connected."}
    msgs = _list("is:unread", max_results)
    if msgs is None:
        return {"error": "Couldn't reach Gmail."}
    return {"new_today": _estimate("is:unread newer_than:1d"), "messages": msgs}


def search_emails(query, max_results=8):
    if not is_connected():
        return {"error": "Google isn't connected."}
    msgs = _list(query, max_results)
    if msgs is None:
        return {"error": "Couldn't reach Gmail."}
    return {"query": query, "messages": msgs}


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", "replace")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;", " ", html)
    html = re.sub(r"&amp;", "&", html)
    return re.sub(r"\s{2,}", " ", html).strip()


def _extract_body(payload) -> str:
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    if mime == "text/plain" and body.get("data"):
        return _decode(body["data"])
    if mime.startswith("multipart"):
        html = None
        for part in payload.get("parts", []):
            t = _extract_body(part)
            if part.get("mimeType") == "text/plain" and t:
                return t
            if part.get("mimeType") == "text/html" and t and html is None:
                html = t
        if html:
            return _strip_html(html)
    if mime == "text/html" and body.get("data"):
        return _strip_html(_decode(body["data"]))
    if body.get("data"):
        return _decode(body["data"])
    return ""


def read_email(query, max_chars=4000):
    """Full text of the single best-matching email, for summarising/answering."""
    if not is_connected():
        return {"error": "Google isn't connected."}
    g = _service("gmail", "v1")
    if not g:
        return {"error": "Couldn't reach Gmail."}
    res = g.users().messages().list(userId="me", q=query, maxResults=1).execute()
    msgs = res.get("messages", [])
    if not msgs:
        return {"error": f"No email found matching '{query}'."}
    d = g.users().messages().get(userId="me", id=msgs[0]["id"], format="full").execute()
    h = _headers(d)
    body = _extract_body(d.get("payload", {})) or (d.get("snippet") or "")
    return {
        "from": _short_from(h.get("from", "")),
        "from_email": _email_addr(h.get("from", "")),
        "subject": h.get("subject", "(no subject)"),
        "date": h.get("date", ""),
        "body": body[:max_chars],
    }


# ── Gmail: send ─────────────────────────────────────────────────────────────
def send_email(to: str, subject: str, body: str, attachments=None) -> dict:
    """Send an email as the operator. (Called only after his explicit approval.)
    `attachments` = list of {filename, mime, data(bytes)} to attach."""
    if not is_connected():
        return {"error": "Google isn't connected."}
    g = _service("gmail", "v1")
    if not g:
        return {"error": "Couldn't reach Gmail."}
    if not (to or "").strip():
        return {"error": "No recipient."}
    try:
        from email.mime.text import MIMEText
        atts = [a for a in (attachments or []) if a and a.get("data")]
        if atts:
            from email.mime.multipart import MIMEMultipart
            from email.mime.application import MIMEApplication
            msg = MIMEMultipart()
            msg.attach(MIMEText(body or ""))
            for a in atts:
                part = MIMEApplication(a["data"], _subtype="octet-stream")
                part.add_header("Content-Disposition", "attachment",
                                filename=a.get("filename") or "attachment")
                if a.get("mime"):
                    part.replace_header("Content-Type", a["mime"])
                msg.attach(part)
        else:
            msg = MIMEText(body or "")
        msg["to"] = to
        msg["subject"] = subject or "(no subject)"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        sent = g.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"ok": True, "id": sent.get("id")}
    except Exception as e:
        logger.warning(f"[google] send failed: {e}")
        return {"error": f"Send failed: {e}"}


# ── Contacts (look up people by name) ───────────────────────────────────────
def search_contacts(name: str, max_results=5):
    """Look up saved contacts AND people he's emailed before, by name.
    Returns [{name, email}] (deduped). Empty list if not connected / no People
    scope granted yet / no match."""
    if not is_connected():
        return []
    p = _service("people", "v1")
    if not p:
        return []
    q = (name or "").strip()
    if not q:
        return []
    out, seen = [], set()

    def _collect(results):
        for r in results or []:
            person = r.get("person", r)
            dn = ""
            names = person.get("names") or []
            if names:
                dn = names[0].get("displayName", "")
            for e in person.get("emailAddresses") or []:
                addr = (e.get("value") or "").strip()
                if addr and addr.lower() not in seen:
                    seen.add(addr.lower())
                    out.append({"name": dn or addr, "email": addr})

    try:
        r1 = p.people().searchContacts(
            query=q, readMask="names,emailAddresses", pageSize=max_results).execute()
        _collect(r1.get("results"))
    except Exception as e:
        logger.warning(f"[google] searchContacts failed: {e}")
    try:
        r2 = p.otherContacts().search(
            query=q, readMask="names,emailAddresses", pageSize=max_results).execute()
        _collect(r2.get("results"))
    except Exception as e:
        logger.warning(f"[google] otherContacts.search failed: {e}")
    return out[:max_results]


def resolve_recipients(items):
    """Turn a mix of email addresses and names into addresses via Contacts.
    Returns {resolved:[emails], ambiguous:{name:[{name,email}]}, unresolved:[names]}.
    A token containing '@' is treated as an address and passed straight through."""
    resolved, ambiguous, unresolved = [], {}, []
    for it in items or []:
        tok = (it or "").strip()
        if not tok:
            continue
        if "@" in tok:
            resolved.append(tok)
            continue
        matches = search_contacts(tok, max_results=5)
        if not matches:
            unresolved.append(tok)
        elif len(matches) == 1:
            resolved.append(matches[0]["email"])
        else:
            exact = [m for m in matches if m["name"].lower() == tok.lower()]
            if len(exact) == 1:
                resolved.append(exact[0]["email"])
            else:
                ambiguous[tok] = matches
    return {"resolved": resolved, "ambiguous": ambiguous, "unresolved": unresolved}


# ── Calendar ────────────────────────────────────────────────────────────────
CALENDAR_TZ = os.environ.get("GRACE_CALENDAR_TZ", "Africa/Lagos")


def create_event(summary, start, end="", location="", description="", attendees=None) -> dict:
    """Create a Google Calendar event. start/end are 'YYYY-MM-DD HH:MM' (local);
    if end is blank, defaults to start + 1 hour. `attendees` = list of email
    addresses to invite (they get a Google Calendar invitation)."""
    if not is_connected():
        return {"error": "Google isn't connected."}
    cal = _service("calendar", "v3")
    if not cal:
        return {"error": "Couldn't reach Calendar."}
    try:
        s = datetime.strptime(start.strip(), "%Y-%m-%d %H:%M")
    except Exception:
        return {"error": "I couldn't read the start time — need YYYY-MM-DD HH:MM."}
    if end and end.strip():
        try:
            e = datetime.strptime(end.strip(), "%Y-%m-%d %H:%M")
        except Exception:
            e = s + timedelta(hours=1)
    else:
        e = s + timedelta(hours=1)
    body = {
        "summary": summary or "(untitled)",
        "start": {"dateTime": s.isoformat(), "timeZone": CALENDAR_TZ},
        "end": {"dateTime": e.isoformat(), "timeZone": CALENDAR_TZ},
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    invited = [a.strip() for a in (attendees or []) if a and "@" in a]
    if invited:
        body["attendees"] = [{"email": a} for a in invited]
    try:
        created = cal.events().insert(
            calendarId="primary", body=body,
            sendUpdates="all" if invited else "none").execute()
        return {"ok": True, "id": created.get("id"), "link": created.get("htmlLink"),
                "when": s.strftime("%a %b %d, %I:%M %p").replace(" 0", " "),
                "invited": invited}
    except Exception as ex:
        logger.warning(f"[google] create_event failed: {ex}")
        return {"error": f"Couldn't create the event: {ex}"}


def upcoming_events(days=14, max_results=8):
    if not is_connected():
        return {"error": "Google isn't connected."}
    cal = _service("calendar", "v3")
    if not cal:
        return {"error": "Couldn't reach Calendar."}
    now = datetime.now(timezone.utc)
    tmax = (now + timedelta(days=days)).isoformat()
    res = cal.events().list(
        calendarId="primary", timeMin=now.isoformat(), timeMax=tmax,
        maxResults=max_results, singleEvents=True, orderBy="startTime").execute()
    out = []
    for e in res.get("items", []):
        start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "")
        out.append({"summary": e.get("summary", "(no title)"), "start": start,
                    "location": e.get("location", "")})
    return {"events": out}


# ── Gmail: inbox actions (mark read / archive / trash) ──────────────────────
def modify_emails(query: str, action: str, max_results=25) -> dict:
    """Apply an action to all messages matching a Gmail query.
    action: 'mark_read' | 'archive' | 'trash'."""
    if not is_connected():
        return {"error": "Google isn't connected."}
    g = _service("gmail", "v1")
    if not g:
        return {"error": "Couldn't reach Gmail."}
    if not (query or "").strip():
        return {"error": "Need a query (e.g. 'from:linkedin', 'category:promotions')."}
    try:
        res = g.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        ids = [m["id"] for m in res.get("messages", [])]
        if not ids:
            return {"count": 0, "action": action, "query": query}
        if action == "mark_read":
            g.users().messages().batchModify(userId="me", body={"ids": ids, "removeLabelIds": ["UNREAD"]}).execute()
        elif action == "archive":
            g.users().messages().batchModify(userId="me", body={"ids": ids, "removeLabelIds": ["INBOX"]}).execute()
        elif action == "trash":
            for mid in ids:
                g.users().messages().trash(userId="me", id=mid).execute()
        else:
            return {"error": f"Unknown action '{action}'."}
        return {"count": len(ids), "action": action, "query": query}
    except Exception as e:
        logger.warning(f"[google] modify_emails failed: {e}")
        return {"error": f"Action failed: {e}"}


# ── Calendar: manage existing events ────────────────────────────────────────
TZ_OFFSET = os.environ.get("GRACE_TZ_OFFSET", "+01:00")   # Africa/Lagos, no DST


def _find_event(query: str, days=45):
    """(calendar_service, event) for the soonest upcoming event whose title
    contains `query`, or (service, None)."""
    cal = _service("calendar", "v3")
    if not cal:
        return None, None
    now = datetime.now(timezone.utc)
    tmax = (now + timedelta(days=days)).isoformat()
    res = cal.events().list(calendarId="primary", timeMin=now.isoformat(), timeMax=tmax,
                            maxResults=50, singleEvents=True, orderBy="startTime").execute()
    q = (query or "").lower().strip()
    for e in res.get("items", []):
        if q and q in (e.get("summary", "") or "").lower():
            return cal, e
    return cal, None


def reschedule_event(query, new_start, new_end="") -> dict:
    if not is_connected():
        return {"error": "Google isn't connected."}
    cal, e = _find_event(query)
    if not cal:
        return {"error": "Couldn't reach Calendar."}
    if not e:
        return {"error": f"No upcoming event matching '{query}'."}
    try:
        s = datetime.strptime(new_start.strip(), "%Y-%m-%d %H:%M")
    except Exception:
        return {"error": "Need the new start as YYYY-MM-DD HH:MM."}
    # preserve original duration unless a new end is given
    dur = timedelta(hours=1)
    try:
        os_ = e["start"].get("dateTime"); oe_ = e["end"].get("dateTime")
        if os_ and oe_:
            dur = datetime.fromisoformat(oe_) - datetime.fromisoformat(os_)
    except Exception:
        pass
    if new_end and new_end.strip():
        try:
            en = datetime.strptime(new_end.strip(), "%Y-%m-%d %H:%M")
        except Exception:
            en = s + dur
    else:
        en = s + dur
    e["start"] = {"dateTime": s.isoformat(), "timeZone": CALENDAR_TZ}
    e["end"] = {"dateTime": en.isoformat(), "timeZone": CALENDAR_TZ}
    try:
        cal.events().update(calendarId="primary", eventId=e["id"], body=e, sendUpdates="all").execute()
        return {"ok": True, "summary": e.get("summary", "event"),
                "when": s.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")}
    except Exception as ex:
        return {"error": f"Couldn't reschedule: {ex}"}


def cancel_event(query) -> dict:
    if not is_connected():
        return {"error": "Google isn't connected."}
    cal, e = _find_event(query)
    if not cal:
        return {"error": "Couldn't reach Calendar."}
    if not e:
        return {"error": f"No upcoming event matching '{query}'."}
    try:
        cal.events().delete(calendarId="primary", eventId=e["id"], sendUpdates="all").execute()
        return {"ok": True, "summary": e.get("summary", "event")}
    except Exception as ex:
        return {"error": f"Couldn't cancel: {ex}"}


def check_availability(start, end) -> dict:
    """Free/busy over a window, computed from the events list (works with the
    calendar.events scope — the freeBusy endpoint needs a broader scope)."""
    if not is_connected():
        return {"error": "Google isn't connected."}
    cal = _service("calendar", "v3")
    if not cal:
        return {"error": "Couldn't reach Calendar."}
    try:
        s = datetime.strptime(start.strip(), "%Y-%m-%d %H:%M")
        en = datetime.strptime(end.strip(), "%Y-%m-%d %H:%M")
    except Exception:
        return {"error": "Need start and end as YYYY-MM-DD HH:MM."}
    try:
        res = cal.events().list(
            calendarId="primary", timeMin=s.isoformat() + TZ_OFFSET,
            timeMax=en.isoformat() + TZ_OFFSET, singleEvents=True,
            orderBy="startTime", maxResults=20).execute()
        busy = []
        for e in res.get("items", []):
            if e.get("transparency") == "transparent":   # marked "free"
                continue
            st = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "")
            busy.append({"summary": e.get("summary", "(busy)"), "start": st})
        return {"free": len(busy) == 0, "busy": busy,
                "window": f"{s.strftime('%a %b %d %I:%M %p')} – {en.strftime('%I:%M %p')}".replace(" 0", " ")}
    except Exception as ex:
        return {"error": f"Couldn't check availability: {ex}"}


# ── Format helpers for the model / briefing ─────────────────────────────────
def format_unread(d: dict) -> str:
    if "error" in d:
        return d["error"]
    msgs = d.get("messages", [])
    if not msgs:
        return "Inbox is clear — no unread mail."
    nt = d.get("new_today", -1)
    head = (f"About {nt} new email(s) in the last day. Latest:" if nt and nt > 0
            else "Your most recent unread emails:")
    lines = [head]
    for m in msgs:
        lines.append(f"- {m['from']}: {m['subject']}" + (f" — {m['snippet'][:80]}" if m['snippet'] else ""))
    return "\n".join(lines)


def format_search(d: dict) -> str:
    if "error" in d:
        return d["error"]
    msgs = d.get("messages", [])
    if not msgs:
        return f"No emails found for '{d.get('query','')}'."
    lines = [f"Emails matching '{d.get('query','')}':"]
    for m in msgs:
        lines.append(f"- {m['from']}: {m['subject']} ({m['date']})" + (f" — {m['snippet'][:80]}" if m['snippet'] else ""))
    return "\n".join(lines)


def format_email(d: dict) -> str:
    if "error" in d:
        return d["error"]
    addr = f" <{d['from_email']}>" if d.get("from_email") else ""
    return (f"Email from {d['from']}{addr} — \"{d['subject']}\" ({d['date']}):\n\n{d['body']}"
            f"\n\n[To reply, draft_email to {d.get('from_email','')} with subject "
            f"\"Re: {d['subject']}\".]")


def format_events(d: dict) -> str:
    if "error" in d:
        return d["error"]
    evs = d.get("events", [])
    if not evs:
        return "Nothing on your Google Calendar in that window."
    lines = ["Upcoming on your Google Calendar:"]
    for e in evs:
        loc = f" @ {e['location']}" if e.get("location") else ""
        lines.append(f"- {e['start']}: {e['summary']}{loc}")
    return "\n".join(lines)
