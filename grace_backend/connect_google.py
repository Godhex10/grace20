"""One-time Google consent (manual copy-paste flow).

Run this locally. It prints an AUTH_URL — open it, approve, and your browser
lands on a "localhost refused to connect" page. THAT IS FINE: the page you land
on has the authorization code in its address bar. Copy that whole URL (or just
the code) and paste it back here. We exchange it for a refresh token and save it
to google_token.json, which then works from anywhere (including the server).

This avoids relying on a local server catching the redirect (which can fail
behind firewalls / when the port is busy)."""
import os
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))   # load creds regardless of CWD
TOKEN_PATH = os.path.join(HERE, "google_token.json")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",   # read + archive/mark-read/label/trash
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/contacts.readonly",        # look up people by name
    "https://www.googleapis.com/auth/contacts.other.readonly",  # + people he's emailed before
]
CLIENT_CONFIG = {
    "installed": {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES)
flow.redirect_uri = "http://localhost"   # loopback; the browser just needs to land here

auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
print("\n1) Open this URL in your browser and approve:\n")
print("AUTH_URL:", auth_url)
print(
    "\n2) After you click Allow, the browser will show a 'localhost refused to "
    "connect' page — that's expected. Copy the ENTIRE URL from the address bar "
    "(it looks like http://localhost/?state=...&code=4/0A...&scope=...).\n"
)

pasted = input("3) Paste that URL (or just the code) here, then press Enter:\n> ").strip()

# Accept either the full redirect URL or a bare code.
code = pasted
if "code=" in pasted:
    qs = parse_qs(urlparse(pasted).query)
    code = qs.get("code", [pasted])[0]

flow.fetch_token(code=code)
creds = flow.credentials
with open(TOKEN_PATH, "w", encoding="utf-8") as f:
    f.write(creds.to_json())
print("\nTOKEN_SAVED", TOKEN_PATH)
print("Done — Grace now has Gmail, Calendar, and Contacts access.")
