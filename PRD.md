# 📑 G.R.A.C.E. — Product Requirements Document (v2, updated)

**Product:** G.R.A.C.E. — *Genuinely Reliable Assistant for Command & Execution*
**Architecture:** Pageless, grid-mapped contextual workspace
**Target:** Web-first SPA (Chrome/Blink), now **cloud-hosted with HTTPS + login** → future native desktop (Electron)
**Status legend:** ✅ Done · ⚠️ Partial · 🔄 Changed by design · ❌ Not yet

> This v2 supersedes the original PRD. The original described a local AI command
> center focused on widget orchestration, code editing, and system security.
> The product has since evolved primarily into a **proactive, memory-driven AI
> companion/assistant** (FRIDAY-style), and is now **hosted on the cloud** with a
> Postgres database, HTTPS, and password login. This doc records what actually
> exists, where it diverged from the original spec, and what's still outstanding.

---

## 🧠 1. Product Overview & Persona

G.R.A.C.E. is a local-first → now **cloud-hosted**, hybrid AI command center — a
single-page fluid workspace where independent UI widgets spawn and dismiss via
text and **voice** commands. ✅

**Persona — 🔄 changed by design.** The original spec called for a persona
"devoid of aggressive sci-fi tropes." Per operator direction, Grace is now a
**FRIDAY-style companion**: casually warm, lightly sassy, emotionally present,
proactive, unflappable — a sharp co-pilot and friend, not a butler. She addresses
the operator as "Boss." ✅

---

## 🎯 2. Core Architecture & Stack

| Layer | Spec | Status |
|---|---|---|
| Frontend shell | HTML5 + Vanilla JS (ES6+) | ✅ (single `index.html`) |
| CSS / layout | Bootstrap 5 + **Gridstack.js** | 🔄 Custom CSS + custom drag engine (no Bootstrap/Gridstack) |
| Backend | Python + FastAPI | ✅ (`127.0.0.1:8000`, reverse-proxied in production) |
| Database | SQLite via ORM (SQLAlchemy) | 🔄 ✅ **PostgreSQL (Supabase) in production**; SQLite fallback locally. Same SQLAlchemy models, dual-mode via `DATABASE_URL`. |
| Intelligence | Two-tier router: local regex + Gemini API | ✅ |
| Realtime | Server-Sent Events (SSE) | ✅ (one shared channel — see §3) |
| Voice out | edge-tts (streaming + cache) | 🔄 ✅ **Azure Speech (Sonia) primary — much faster; edge-tts automatic fallback** |
| Voice in | Web Speech API | ✅ **Done** — wake word "Grace", hands-free follow-up window, echo prevention (see §5) |
| Hosting | *(originally: local only)* | ✅ **Oracle Cloud VM + Caddy reverse proxy, auto-HTTPS, basic-auth login** (see §8) |

---

## 🧱 3. Widget Lifecycle & Routing

- **Tier-1 fast path** (local regex/keyword, sub-ms): briefing, weekly review,
  date/time, weather, diagnostics, lock, open-vscode. ✅
- **Tier-2 Gemini fallback** with live "typing" state (token streaming). ✅
- **Context evaluation (Visual vs Auditory), Scenario B "Fullscreen Focus Mode"**
  — suppress visual spawns during focus, hold widgets in a background queue,
  fall back to voice-only. ❌ **Not implemented.** `layout_context` is passed to
  the backend but focus-mode suppression/queueing is not built. *(Deferred by
  operator.)*
- **SSE transport** — 🔄 Changed. The spec called for **per-widget channels**
  (`/api/stream/{widget_id}`). Implemented instead as **one shared channel**
  (`/api/workspace/stream`) with a `target_widget` field per packet, fanned out
  per connection. Simpler and works well for a single operator. In production the
  proxy (Caddy) is configured with `flush_interval -1` so SSE streams unbuffered.
- **Widget teardown** — ⚠️ Partial. Widgets close with animation + DOM hide and
  the shared SSE is torn down/reconnected as one; there is no per-widget socket
  destructor (not needed with the shared-channel design).

---

## 🗂️ 4. Database Schema

**Now PostgreSQL (Supabase) in production, SQLite locally** — same SQLAlchemy
models, selected at runtime by `DATABASE_URL` (`postgresql://` is normalised to
`postgresql+psycopg://`; pooled connection with pre-ping + recycle). 🔄 ✅

Original tables — all present (and extended):
- `tasks` ✅ (extended: description, priority, tag, `inprog` status, checklist,
  `due_date`, **`completed_at`**)
- `interaction_log` ✅ (self-pruning to the most recent 300 rows). The chat
  history now **loads the last conversations back into the UI** on open (no more
  hardcoded demo text). ✅
- `long_term_memory` ✅

**Added well beyond the original spec:**
- `calendar_events` — calendar with full CRUD
- `projects` — project momentum / anti-stall tracking
- `conversation_memory` — episodic memory (gist + emotional tone of past talks)
- `reminders` — one-off reminders & timers
- `notes` — quick idea/note capture, filed by lane
- `habits` — recurring daily habits (name, icon, cached longest streak)
- `habit_checkins` — one row per habit per local day; the current streak is
  computed from these (consecutive days), so it survives restarts/timezone drift

---

## ✨ 5. Capabilities Inventory (what she can do today)

### Work management
- **Tasks / Kanban** — create, delete, move between columns, re-prioritise,
  rename, re-tag — via board **and** chat; live SSE sync. ✅
- **Calendar** — add/edit/delete events, persistent, past-date auto-correct;
  via widget **and** chat. ✅
- **Projects / Momentum** — track projects, next actions, and "days since
  progress"; **anti-stall engine** flags stalling backend work and nudges. ✅
- **Notes** — capture & recall ideas by category (dev/creative/personal). ✅
- **Reminders & timers** — "remind me to X at Y", "25-min timer"; fires
  proactively with voice + text. ✅
- **Habits & streaks** — track recurring daily habits; check them off via the
  Habits widget (green check circle) **or** chat ("I did my run today"); Grace
  counts consecutive-day **streaks** (🔥), keeps a per-habit personal best, and
  nudges on pending ones in the morning briefing. A streak stays "alive but at
  risk" through the current day and only resets after a day fully lapses.
  ✅ (daily cadence; per-weekday schedules not yet built)

### Email & Calendar — Google integration ✅ (NEW)
Connected to the operator's real Gmail + Google Calendar via OAuth (a one-time
consent saves a refresh token; works from the hosted server). All sending/editing
is **human-in-the-loop**.
- **Read & triage mail** — summarise unread / "what's new", search the inbox,
  and read/summarise a single message. Fetches are **batched** for speed. ✅
- **Send & reply (approve-first)** — Grace *drafts* an email into a **Review &
  Send card**; nothing leaves until the operator clicks **Send**. Replies pull
  the original sender + subject automatically. ✅
- **Attachments** — "summarise this PDF and email it to X" → Grace builds the
  summary document and attaches it to the draft; the card shows a 📎 chip so the
  operator sees the attachment before sending. ✅ (NEW)
- **Invite / email people by name** — via **Google Contacts** (People API):
  names resolve to addresses automatically (saved contacts **and** people he's
  emailed before). Ambiguous names → Grace asks which; unknown names → she asks
  for the address instead of guessing. ✅ (NEW)
- **Calendar CRUD + invitations** — create events (resolves "Friday 3pm" to an
  exact time), **invite attendees** (by email or name), reschedule, cancel
  (guests notified), and **check availability** over a window. ✅
- **Inbox actions** — archive / mark-read / trash all mail matching a Gmail
  query ("archive those newsletters"). ✅

### Intelligence & memory
- **Two kinds of memory** — long-term facts + **episodic conversation memory**
  (auto-summarised, recalled across sessions). ✅
- **Temporal awareness** — knows current time and how long since you last spoke;
  greets correctly across sessions/days. ✅
- **Daily planning** — reasons over board, calendar, stalls, and your state to
  give a real prioritised call, not a list. ✅
- **File/document reading** — reads uploaded PDFs, images (vision), text/code;
  answers questions, extracts data. ✅
- **Multi-file context** — up to **6 files** attached at once; Grace reasons
  **across** all of them (compare two contracts, summarise three notes,
  cross-reference a PDF and a spreadsheet). Each file is labeled by name; per-file
  remove in the panel; re-uploading a name replaces it; oldest drops past the cap. ✅
- **Web search** — Tavily-backed, synthesised answers. ✅
- **Weather** — live (Open-Meteo), streaming, in chat + widget + briefing. ✅

### Proactive (she reaches out first)
- **Daily briefing** (morning) — greeting, weather, calendar, tasks. ✅
- **Anti-stall nudge** (afternoon) — the stalest backend item. ✅
- **Warm check-in** (evening) — memory-aware, in her voice. ✅
- **Weekly review** (Sunday) — honest recap of what shipped + what's slipping. ✅

### Code / debugging
- **Read-only code workspace** — browse the project (`list_files`), open & read
  files into the Code Panel (`read_code_file`), and grep across the project
  (`search_code`). Sandboxed to the GRACE project root; every path is checked to
  stay inside (no `..` escapes). ✅
- **Debugging** — she reads the actual file and finds bugs with line numbers,
  explains code. ✅
- **Auto-documentation** — "document this" / "write JSDoc" / "add a docstring"
  → she reads the code and generates, in one shot, three outputs: the inline doc
  comment (correct style per language), a README section, and a runnable usage
  example. ✅
- **Deep mode** — code/debugging/doc tasks auto-switch to a stronger model
  (`GRACE_CODE_MODEL`, e.g. gemini-3.6-flash) with a large token budget and a
  thorough-analysis directive, while normal chat stays fast on the light model.
  Falls back to the base model if the deep one is briefly unavailable. ✅
- **Editing with approval (Phase B)** — she can *propose* a fix (`propose_edit`,
  surgical old→new replacement). It stages a **diff for review** in a modal;
  nothing is written until you click **Apply**, which backs up the original to
  `.grace/backups/` first. Reject discards it. Human-in-the-loop, sandboxed,
  reversible. ✅

### Documents (create / edit)
- **Document editor widget** — upload a `.txt`, Word `.docx`, or **PDF** and it
  opens in an editable text editor (DOCX/PDF text is extracted server-side). ✅
- **Live editing by chat** — "find X and change it to Y", "rewrite this
  paragraph" → the edit applies **live** in the editor and is **undoable**
  (Ctrl+Z, via the browser's native undo). ✅
- **Summarise / transform → downloadable file** — "summarise this into a new
  document", "translate this" → Grace generates a **new file** and pops a
  **"Document ready"** toast with a Download button. Output format **matches the
  input** (TXT→TXT, DOCX→DOCX, PDF→PDF). A generated file can also be **emailed
  as an attachment** (see Email above). ✅
- **Auto-open upload** — asking her to act on a document with nothing attached
  ("summarise this pdf") **opens the upload panel** and asks for the file, rather
  than guessing. ✅
- **PDF caveat** — editing is **text-level**: extract → edit → clean, freshly
  typeset output. The original PDF's exact layout/fonts/columns are **not**
  reproduced. Scanned/image PDFs (no text layer) fall back to vision (read-only). ✅

### Map / navigation
- **Host geolocation** — "where is X hosted" → resolves a domain/IP, pins the host
  on the tactical map, gives hosting org/ISP/ASN/reverse-DNS (free, no key). ✅
- **Traffic-aware directions** — "how long from Ikeja to VI?" → geocodes both
  places, routes with LIVE traffic (TomTom), reports distance + ETA + delay, and
  draws the route on the map. Needs `TOMTOM_API_KEY`; degrades gracefully. ✅
- **Directions card + traffic on the map** — a draggable **route card** pops with
  ETA, distance, delay level, "leave-by" time, and alternatives; the route line
  is **colored by congestion** (orange = moderate, red = heavy) and the live
  traffic overlay auto-enables so surrounding roads show too. ✅

### Security / recon
- **Shodan host lookup (OSINT)** — "profile 8.8.8.8" / "check comepayapp.com
  exposure" → passive lookup of Shodan's index: open ports, services, org/ISP/
  ASN, geolocation, and known CVEs. Drops a pin on the tactical map. Needs
  `SHODAN_API_KEY`; degrades gracefully without one. ✅

### Interface & voice
- **Widget control by chat** — "close all widgets", "open the calendar". ✅
- **Voice output** — **Azure Speech (Sonia, en-GB)** primary — streamed +
  line-cached, markdown/symbol sanitised, says "Grace" not the acronym; **edge-tts
  is the automatic fallback**. Azure is several times faster (10–19× on longer
  replies). ✅ 🔄
- **Voice input** — Web Speech API. Click the mic to toggle continuous listening;
  say the **wake word "Grace"** to engage, then a **hands-free follow-up window**
  keeps the conversation going without repeating the wake word. The mic **pauses
  while Grace is speaking** (echo prevention) and resumes after. Works with
  Bluetooth headsets. ✅ (NEW)
- **Live text streaming** — replies type out as generated. ✅

---

## 🔊 6. System Audio Integration — ❌ NOT IMPLEMENTED

The spec's **Conditional Process Ducking** is not built:
- Poll the host OS audio mixer before speaking.
- If VoIP/conferencing (Zoom/Teams/WebRTC) is active → stay silent, push a toast.
- If media is playing → attenuate it 70%, speak, then restore.

Today Grace simply plays TTS. (Requires OS-level audio APIs; better suited to the
Electron/native phase.)

---

## 🔐 7. System Access & Security — ⚠️ PARTIAL

- **Read-only code access** — ✅ built (Phase A). Grace can browse/read/search
  the project code, sandboxed to the GRACE root, and debug it — but cannot write.
- **Scoped Trust Tokens** (15-min, project-dir-locked exec tokens; confirm-once
  then bypass) — ❌ not built. Current `system_ops` uses a fixed allow-list
  (lock, open-vscode, diagnostics) with `shell=False`, no arbitrary execution.
- **Shadow Copy & Diff Engine** (staged edits, git-style diff view,
  user-in-the-loop "apply") — ✅ **built (Phase B)**. `propose_edit` stages a
  surgical change; the frontend shows a red/green diff modal; **Apply** writes the
  file after backing up the original to `.grace/backups/`; **Reject** discards.
  Still scoped to the project root and read-guarded.
- **Hosted-app login** — ✅ the public site is behind **HTTP basic-auth** at the
  reverse proxy (see §8). All Google send/edit actions remain approve-first.

These matter most once Grace can execute terminal commands and edit files — i.e.
the **developer-automation** phase, slated for the native-app version.

---

## ☁️ 8. Cloud Hosting & Deployment ✅ (NEW — the "final phase" from the old roadmap)

The originally-last milestone is now shipped:
- **Database** — migrated SQLite → **PostgreSQL on Supabase** (free tier, EU
  region, pooled connection). Local dev still uses SQLite via the same models. ✅
- **Server** — **Oracle Cloud Always-Free VM** (Ubuntu), backend run by a
  **systemd** service (single process so the background scheduler runs once). ✅
- **Reverse proxy + HTTPS** — **Caddy** serves the frontend and proxies `/api`
  and `/static` to the backend, with **automatic HTTPS** (Let's Encrypt) and SSE
  unbuffered (`flush_interval -1`). Domain via DuckDNS. ✅
- **Login** — the whole site is gated by **HTTP basic-auth** (bcrypt hash in the
  Caddyfile). ✅
- **One database, many clients** — web (and a future mobile client) call the same
  hosted API + Postgres, as originally intended. ✅
- **Automated nightly backups** — a `pg_dump` cron on the Oracle VM dumps the
  whole Supabase DB, gzips it, and keeps the newest 7 (Supabase's free tier has
  no automated backups). Script `/home/ubuntu/grace-backups/backup.sh`, runs
  02:30 UTC nightly, uses `postgresql-client-17` (matches the v17 server), logs
  to `backup.log`. Restore: `zcat grace-<ts>.sql.gz | psql "<DATABASE_URL>"`. ✅

**Ops hardening still to do (recommended closeout):**
- **Rotate credentials** that were shared during setup, and make the site login
  password distinct from the database password.
- **Off-VM backup copy** — the nightly dumps currently live on the same VM;
  periodically pull one to the operator's PC / object storage for true safety.
- **Git-based deploy** — replace manual `scp` + restart with `git pull && restart`.

---

## 🚧 9. Outstanding / Roadmap

**Guiding decision (operator):** build out **everything doable locally first**;
**cloud hosting is the LAST step** — *now done* (§8).

**Recently shipped:**
- ✅ Document editor (TXT/DOCX/PDF) + live editing + summarise-to-download (§5)
- ✅ Habits & streaks (widget + chat + briefing nudge)
- ✅ Multi-file upload (up to 6, cross-file reasoning)
- ✅ Directions card + traffic-on-map coloring
- ✅ **Voice input** (Web Speech API) — wake word + hands-free follow-up + echo prevention
- ✅ **Email + Calendar** (Gmail/Calendar via OAuth) — read/summarise/search, send & reply (approve-first), inbox actions, event CRUD + invites, reschedule/cancel, availability
- ✅ **Email attachments** ("summarise this and email it")
- ✅ **Invite / email people by name** (Google Contacts)
- ✅ **Faster voice** — Azure Speech (Sonia) replaced edge-tts as primary
- ✅ **Chat persistence** — conversations load back into the UI
- ✅ **Cloud hosting** — Supabase Postgres + Oracle VM + Caddy HTTPS + login (§8)
- ✅ **Automated nightly DB backups** — `pg_dump` cron on the VM, 7-day retention (§8)

**Still to build:**
1. ❌ **Developer automation** — open projects/apps/URLs, start XAMPP, run
   commands — gated by **scoped trust tokens** (confirm-once). *Operator's #1;
   native-app phase.*
2. ❌ **Focus mode** — fullscreen pop-up suppression + background widget queue
   (§3). *(Deferred by operator.)*
3. ❌ **System audio ducking** (§6) — go quiet during calls (Windows mixer;
   native-app phase).
4. ⏳ **Habits: per-weekday schedules** — small extension to the shipped habits.
5. ⏳ **Ops hardening** — credential rotation, off-VM backup copy, git-based deploy (§8).

**Optional / nice-to-have:**
- Phone notifications (WhatsApp/Telegram) for reminders & briefings.
- Barge-in voice (interrupt her mid-sentence by talking).
- 🔄 **Cloud voice** — the old "ElevenLabs/Cartesia" idea was evaluated and
  **dropped as too expensive**; **Azure Speech** was chosen instead and is live.

**Design note (`🔄`):** Gridstack.js / Bootstrap 5 were replaced with a custom
CSS + drag engine — by design, not outstanding.

---

*Updated to reflect the current build. Grace has grown from a widget/exec command
center into a proactive, memory-driven AI companion with document editing,
habits, recon, traffic-aware navigation, full Gmail/Calendar integration (with
attachments and contact-by-name), voice input, and faster Azure voice — now
**hosted on the cloud** with Postgres, HTTPS, and login. The remaining work is
developer automation (trust tokens) and OS-level integrations in the native-app
phase, plus light ops hardening.*
