# G.R.A.C.E.

**Genuinely Reliable Assistant for Command & Execution** — a proactive,
memory-driven AI companion (FRIDAY-style) that lives in a single-page, widget-based
workspace. Talk or type; she runs your board, calendar, email, projects, habits,
documents, code, maps, and more — and reaches out first with briefings and nudges.

Built with a **FastAPI** backend and a **vanilla-JS single-page frontend**, powered
by **Google Gemini**, with real-time push over **Server-Sent Events**. Local-first,
now also **cloud-hosted** with Postgres, HTTPS, and login.

---

## ✨ What she can do

- **Work management** — Kanban tasks, calendar (CRUD), projects with an anti-stall
  engine, notes, reminders & timers, and daily **habits with streaks** 🔥.
- **Email & Calendar (Gmail/Google Calendar)** — read/summarise/search mail; draft &
  send/reply (approve-first); **attach generated documents**; invite or email people
  **by name** via Google Contacts; create/reschedule/cancel events; check availability;
  tidy the inbox (archive/mark-read/trash).
- **Documents** — upload `.txt` / `.docx` / `.pdf` into a live editor; edit by chat;
  summarise/translate/rewrite into a **downloadable file** (and email it as an attachment).
- **Intelligence & memory** — long-term facts + episodic conversation memory, temporal
  awareness, real prioritised daily planning, multi-file reasoning (up to 6 files),
  Tavily web search, and live weather.
- **Code / debugging** — browse, read, and grep the project (sandboxed); find bugs with
  line numbers; auto-document; and propose edits as a reviewable diff (apply-on-approval).
- **Maps / recon** — traffic-aware directions with a route card + congestion-colored
  route; host geolocation; Shodan OSINT host lookups pinned to a tactical map.
- **Voice** — **Azure Speech** output (fast, natural) with an edge-tts fallback, and
  **voice input** via the Web Speech API (wake word "Grace", hands-free follow-up,
  echo prevention).
- **Proactive** — morning briefing, afternoon anti-stall nudge, evening check-in,
  Sunday weekly review.

See [`PRD.md`](PRD.md) for the full, current product spec.

---

## 🧱 Stack

| Layer | Tech |
|---|---|
| Backend | Python + FastAPI (Uvicorn) |
| Frontend | Single `index.html` + `styles.css`, vanilla ES6+ |
| AI | Google Gemini (two-tier: local regex fast-path + Gemini reasoning) |
| Realtime | Server-Sent Events (one shared channel) |
| Database | PostgreSQL (Supabase) in production; SQLite locally — same SQLAlchemy models |
| Voice out | Azure Speech (Sonia) primary, edge-tts fallback |
| Voice in | Web Speech API |
| Hosting | Oracle Cloud VM + Caddy (auto-HTTPS) + basic-auth |

---

## 🚀 Running locally

Requires **Python 3.11+**.

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp grace_backend/.env.example grace_backend/.env
#    then edit grace_backend/.env and fill in the keys you want
#    (every key is optional — the app runs with fewer features without them)

# 3. Start the backend
cd grace_backend
uvicorn main:app --host 127.0.0.1 --port 8000

# 4. Open the frontend
#    serve index.html (e.g. with a static server) or open it directly
```

The frontend auto-detects the API base (same-origin when served together).

### Google (Gmail / Calendar / Contacts) — optional

Requires a Google OAuth **Desktop** client (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`
in `.env`) and the Gmail, Calendar, and People APIs enabled. Then run the one-time
consent, which saves a refresh token:

```bash
cd grace_backend
python -u connect_google.py   # follow the printed steps
```

---

## 🔑 Environment variables

All optional — Grace degrades gracefully without any of them. See
[`grace_backend/.env.example`](grace_backend/.env.example) for the template.

| Variable | Enables |
|---|---|
| `GEMINI_API_KEY` | Conversational AI (Gemini) |
| `GRACE_MODEL` / `GRACE_CODE_MODEL` | Model overrides (chat / deep-code mode) |
| `TAVILY_API_KEY` | Live web search |
| `TOMTOM_API_KEY` | Traffic-aware directions + map overlay |
| `SHODAN_API_KEY` | Host OSINT lookups |
| `AZURE_SPEECH_KEY` / `AZURE_SPEECH_REGION` | Fast Azure voice output |
| `DATABASE_URL` | Postgres (Supabase) instead of local SQLite |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Gmail / Calendar / Contacts |

> **Never commit `.env` or `google_token.json`** — they're gitignored for a reason.

---

## 📦 Deployment

See [`grace_backend/deploy/`](grace_backend/deploy/) for the systemd service, Caddyfile,
and `DEPLOY.md`. The production setup runs the backend behind Caddy (auto-HTTPS +
basic-auth), backed by Supabase Postgres, with a nightly `pg_dump` backup on the host.

---

*A personal project — a single-operator AI co-pilot.*
