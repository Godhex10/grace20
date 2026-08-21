# Plan: Switch G.R.A.C.E. Tier 2 from Claude → Google Gemini

## Goal
Replace the Anthropic-backed reasoning engine with Google Gemini 2.5 Flash to
reduce reply latency and remove the dependency on the third-party
`agentrouter.org` proxy (which only served the slow `opus-4-8`). Keep web search,
TTS, SSE streaming, and all Tier 1 behavior exactly as-is.

## Scope
Only the Anthropic-specific glue in `grace_backend/routers/router_pipeline.py`
changes. `services/web_search.py`, `services/audio.py`, `core/sse.py`, and the
frontend are untouched.

## Changes

### 1. Dependencies (`requirements.txt`)
- Add `google-genai` (the current Google Gen AI SDK).
- Leave `anthropic` in place for now (harmless; can remove later).

### 2. Config (`.env` / `.env.example`)
- New var `GEMINI_API_KEY` (user will supply).
- New var `GRACE_MODEL` default becomes `gemini-2.5-flash` (was `claude-opus-4-8`).
- Anthropic vars become unused but are left documented for fallback.

### 3. Client setup (`router_pipeline.py`, ~lines 30–76)
- Replace the `AsyncAnthropic` import + `_build_llm_client()` with a Gemini
  client built from `google.genai`.
- Read `GEMINI_API_KEY` lazily/at module load; if absent, `grace_llm_client`
  stays `None` so the existing "not configured" fallback message still works.

### 4. Tool schema (`router_pipeline.py`, `WEB_SEARCH_TOOL`, ~lines 200–220)
- Convert the Anthropic tool JSON shape to Gemini's function-declaration shape.
  Same name (`web_search`), same single `query` string param.

### 5. Reasoning loop (`router_pipeline.py`, `_run_reasoning_loop` +
   `_stream_one_turn`, ~lines 227–314)
- Rewrite to use Gemini's streaming API.
- Preserve the exact external contract so nothing downstream changes:
  - Push `{stream_id, delta}` packets per text chunk (frontend typewriter).
  - Flush finished sentences to TTS via `_speak_chunk` as they complete.
  - Detect Gemini function-call parts; when the model calls `web_search`, run
    `web_search.search()` + `format_for_model()` (UNCHANGED), feed the result
    back, and loop (cap at `_MAX_TOOL_ROUNDS`).
  - Return `(response_text, streamed_audio)` exactly as today.
- Keep the `[searching the web: <query>]` progress delta.

### 6. Persona / system instruction
- Same persona text. Gemini takes it as `system_instruction` on the config
  rather than a `system=` kwarg.

## What does NOT change
- `services/web_search.py` (Tavily) — model-agnostic, stays byte-for-byte.
- TTS, SSE, Tier 1 (date/time, diagnostics, weather, lock, vscode).
- Frontend `index.html` — the SSE packet contract is preserved.

## Verification
1. `python -c "import ast; ast.parse(open(...))"` syntax check.
2. Restart backend, confirm clean startup.
3. Health check `/api/health`.
4. Tier 1 still instant: "what is the time in nigeria".
5. Tier 2 simple: "explain what FastAPI is in one sentence" → streams, speaks.
6. Tier 2 + search: "what's the latest news about NASA" → fires web_search,
   grounds answer in live Tavily results.
7. Compare wall-clock latency vs. the old opus-through-router numbers.

## Rollback
Anthropic code path is replaced, but `anthropic` stays installed and the old
env vars documented, so reverting is a git-less matter of restoring the file
from this session if needed.

## Open item
- Need `GEMINI_API_KEY` from you (https://aistudio.google.com → Get API key).
  I can build everything now and drop the key into `.env` when you paste it.
