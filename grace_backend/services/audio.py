# services/audio.py
import os
import re
import time
import uuid
import hashlib
import logging
import asyncio
import collections
from pathlib import Path

logger = logging.getLogger(__name__)


# Symbols the TTS engine would otherwise pronounce literally ("asterisk",
# "slash", "underscore", …). We strip Markdown formatting and collapse stray
# punctuation so the SPOKEN text sounds natural, while the on-screen chat text
# (delivered separately over SSE) keeps its full formatting.
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")     # [text](url) -> text
# Emphasis with asterisks only: **x** *x* -> x. Underscores are NOT treated as
# emphasis here because snake_case identifiers (create_event) would be mangled;
# they're softened to spaces in a later step instead.
_BOLD_ITALIC_RE = re.compile(r"\*{1,3}(.+?)\*{1,3}")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+", re.MULTILINE)

# Priority codes appear in calendar listings as bracketed tags like "[hi]".
# Spoken literally they sound like "hi"/"med"; expand them to natural phrases.
_PRIORITY_WORD = {"hi": "high", "med": "medium", "low": "low"}
_PRIORITY_SPOKEN = {k: f"{v} priority" for k, v in _PRIORITY_WORD.items()}
_PRIORITY_TAG_RE = re.compile(r"\[\s*(hi|med|low)\s*\]", re.IGNORECASE)
# "G.R.A.C.E." is an acronym but is pronounced as the name "Grace". Without this
# the TTS spells it out letter by letter. Matches the dotted/spaced forms.
_GRACE_ACRONYM_RE = re.compile(r"\bG[.\s]*R[.\s]*A[.\s]*C[.\s]*E\.?", re.IGNORECASE)
# "priority med" / "priority: hi" -> "medium priority" / "high priority".
_PRIORITY_WORD_RE = re.compile(r"\bpriority[:\s]+(hi|med|low)\b", re.IGNORECASE)
# Item id references she reads aloud as "ID 1" — drop them from speech.
# Handles "ID 1:", "(ID 1)", "id 1 -" etc.
_ID_REF_RE = re.compile(r"[\(\[]?\bid\s+\d+\b[\)\]]?\s*[:.\-]?\s*", re.IGNORECASE)
# "tag MISC" / ", tag WORK" — internal categorization, not worth speaking.
# \w+ (not \S+) so a trailing ")" or "]" isn't swallowed with the tag word.
_TAG_REF_RE = re.compile(r",?\s*\btag\s+\w+", re.IGNORECASE)


def _clean_for_speech(text: str) -> str:
    """Return a spoken-friendly version of `text`: Markdown removed and symbols
    that TTS reads aloud by name (*, /, _, #, `) softened to natural speech."""
    if not text:
        return text

    # Remove fenced code blocks entirely — reading code aloud is noise.
    text = _CODE_BLOCK_RE.sub(" ", text)
    # Say "Grace", never spell out "G.R.A.C.E.".
    text = _GRACE_ACRONYM_RE.sub("Grace", text)
    # Expand bracketed priority tags ("[hi]" -> "high priority") before other
    # bracket/markup handling touches them.
    text = _PRIORITY_TAG_RE.sub(lambda m: _PRIORITY_SPOKEN[m.group(1).lower()], text)
    # Unwrap inline formatting, keeping the inner words.
    text = _LINK_RE.sub(r"\1", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _BOLD_ITALIC_RE.sub(r"\1", text)
    # Strip leading heading hashes and list bullets.
    text = _HEADING_RE.sub("", text)
    text = _BULLET_RE.sub("", text)

    # With markdown gone, tidy the task/event read-out into natural English:
    # "priority med" -> "medium priority", and drop "ID 1" refs and "tag XXX".
    text = _PRIORITY_WORD_RE.sub(lambda m: _PRIORITY_SPOKEN[m.group(1).lower()], text)
    text = _TAG_REF_RE.sub("", text)
    text = _ID_REF_RE.sub("", text)

    # Any leftover markup asterisks that weren't part of a pair: drop them.
    text = text.replace("*", "")
    # Underscores are usually snake_case identifiers — speak them as spaces so
    # "create_event" is read "create event", not "createevent".
    text = text.replace("_", " ")
    # Speak "/" and a few separators as a short pause rather than their names.
    text = re.sub(r"\s*/\s*", " ", text)
    text = text.replace("#", "")

    # Square brackets around status labels ("[To Do]") should be spoken as the
    # word inside, not by name — keep the contents, drop the brackets.
    text = text.replace("[", "").replace("]", "")
    # Remove now-empty or dangling parentheses left by tag/id removal, and any
    # stray leading separators (": ", "- ") the deletions exposed.
    text = re.sub(r"\(\s*\)", "", text)          # empty ()
    text = re.sub(r"\(\s*,", "(", text)          # "( ," -> "("
    text = re.sub(r",\s*\)", ")", text)          # ", )" -> ")"
    text = re.sub(r"\s+([)\]])", r"\1", text)    # space before closing
    text = re.sub(r"^[\s:;,\-–—]+", "", text, flags=re.MULTILINE)  # line-leading junk
    text = re.sub(r"\s{2,}", " ", text)          # collapse runs of spaces
    text = re.sub(r"\s+([,.])", r"\1", text)     # space before comma/period
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False
    logger.warning("edge-tts not installed. Voice synthesis will be unavailable (silent).")

# Grace's default voice — Sonia (UK), composed and elegant. Override with
# the GRACE_VOICE env var (any Microsoft neural voice, e.g. en-US-AriaNeural).
GRACE_VOICE = os.environ.get("GRACE_VOICE", "en-GB-SoniaNeural")

# Speech rate relative to normal, e.g. "+20%" faster, "-10%" slower. Override
# with GRACE_VOICE_RATE. Edge-TTS expects a signed percentage string.
GRACE_VOICE_RATE = os.environ.get("GRACE_VOICE_RATE", "+8%")

# ── Azure Speech (fast primary; same neural voices as edge-tts) ──────────────
# When AZURE_SPEECH_KEY + AZURE_SPEECH_REGION are set we synthesize via Azure's
# official API (~0.2-0.4s from the EU server) and fall back to edge-tts on any
# error or monthly-quota exhaustion, so voice never breaks.
AZURE_SPEECH_KEY = os.environ.get("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION = os.environ.get("AZURE_SPEECH_REGION", "")
_AZURE_ENABLED = bool(AZURE_SPEECH_KEY and AZURE_SPEECH_REGION)
_AZURE_FORMAT = "audio-24khz-48kbitrate-mono-mp3"


async def _azure_bytes(text: str) -> bytes:
    """Synthesize `text` via Azure Speech → MP3 bytes. Raises on failure so the
    caller can fall back to edge-tts."""
    import html
    import httpx
    url = f"https://{AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
    ssml = (f"<speak version='1.0' xml:lang='en-GB'><voice name='{GRACE_VOICE}'>"
            f"<prosody rate='{GRACE_VOICE_RATE}'>{html.escape(text)}</prosody>"
            f"</voice></speak>")
    headers = {
        "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": _AZURE_FORMAT,
        "User-Agent": "grace-tts",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, content=ssml.encode("utf-8"), headers=headers)
        r.raise_for_status()
        return r.content


class MisoAudioEngine:
    def __init__(self, output_dir: str = None, max_files: int = 100, max_age_hours: int = 24):
        # Absolute path under the writable runtime dir so it works both in dev and
        # as a packaged .exe (where the CWD may be read-only).
        if output_dir is None:
            try:
                from services import appconfig
                output_dir = os.path.join(appconfig.runtime_dir(), "static", "audio")
            except Exception:
                output_dir = "static/audio"
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_files = max_files
        self.max_age_seconds = max_age_hours * 3600
        self.voice = GRACE_VOICE
        self.rate = GRACE_VOICE_RATE
        self._cleanup_task = None

    async def start_cleanup_task(self):
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

    async def _periodic_cleanup(self):
        while True:
            await asyncio.sleep(3600)
            await self._cleanup_old_files()

    async def _cleanup_old_files(self):
        try:
            now = time.time()
            files = sorted(self.output_dir.glob("grace_voice_*.mp3"), key=lambda f: f.stat().st_mtime)

            for f in files:
                if now - f.stat().st_mtime > self.max_age_seconds:
                    f.unlink(missing_ok=True)

            if len(files) > self.max_files:
                for f in files[:-self.max_files]:
                    f.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Audio cleanup failed: {e}")

    async def generate_speech(self, text: str) -> str:
        if not text or not text.strip():
            return ""

        # Strip Markdown/symbols so the voice doesn't say "asterisk"/"slash".
        text = _clean_for_speech(text)
        if not text:
            return ""

        if len(text) > 5000:
            text = text[:5000] + "..."
            logger.warning("Text truncated to 5000 chars for TTS")

        filename = f"grace_voice_{uuid.uuid4().hex[:12]}.mp3"
        filepath = self.output_dir / filename

        # Azure first (fast), then edge-tts.
        if _AZURE_ENABLED:
            try:
                data = await _azure_bytes(text)
                if data:
                    filepath.write_bytes(data)
                    return f"/static/audio/{filename}"
            except Exception as e:
                logger.warning(f"[voice] Azure (file) failed, edge-tts fallback: {e}")

        if not EDGE_TTS_AVAILABLE:
            logger.warning("Voice requested but edge-tts is unavailable; returning silent.")
            return ""

        try:
            communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)
            await communicate.save(str(filepath))

            logger.info(f"[Voice Engine:{self.voice}@{self.rate}] Synthesized speech for: '{text[:50]}...'")
            return f"/static/audio/{filename}"

        except Exception as e:
            logger.error(f"[Voice Engine Error] Speech synthesis failed: {e}", exc_info=True)
            # Clean up any partial/empty file so it isn't served as broken audio.
            try:
                filepath.unlink(missing_ok=True)
            except Exception:
                pass
            return ""

    # ── Streaming synthesis + line cache ────────────────────────────────────
    # Instead of rendering the whole MP3 before the browser gets anything, we
    # stream edge-tts audio chunks out as they're produced. The browser plays the
    # first chunk (~300ms) while the rest synthesizes — killing most of the
    # post-text delay. Repeated exact lines are served from an in-memory cache
    # instantly (no re-synthesis).
    _cache: "collections.OrderedDict[str, bytes]" = None  # set below
    _CACHE_MAX = 120

    def _cache_key(self, text: str) -> str:
        raw = f"{self.voice}|{self.rate}|{text}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    async def stream_speech(self, text: str):
        """Async generator yielding MP3 bytes for `text`. Serves from cache when
        the exact line was spoken before; otherwise streams from edge-tts and
        caches the result for next time."""
        if MisoAudioEngine._cache is None:
            MisoAudioEngine._cache = collections.OrderedDict()

        text = _clean_for_speech(text)
        if not text:
            return
        if len(text) > 5000:
            text = text[:5000] + "..."

        key = self._cache_key(text)
        cached = MisoAudioEngine._cache.get(key)
        if cached is not None:
            MisoAudioEngine._cache.move_to_end(key)  # LRU refresh
            yield cached
            return

        # 1) Azure (fast) — whole clip in one quick call. Fall back on any failure
        #    (auth/quota errors surface before any bytes are yielded).
        if _AZURE_ENABLED:
            try:
                data = await _azure_bytes(text)
                if data:
                    MisoAudioEngine._cache[key] = data
                    MisoAudioEngine._cache.move_to_end(key)
                    while len(MisoAudioEngine._cache) > self._CACHE_MAX:
                        MisoAudioEngine._cache.popitem(last=False)
                    yield data
                    return
            except Exception as e:
                logger.warning(f"[voice] Azure failed, falling back to edge-tts: {e}")

        # 2) edge-tts (fallback)
        if not EDGE_TTS_AVAILABLE:
            return
        chunks = []
        try:
            communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    data = chunk["data"]
                    chunks.append(data)
                    yield data
        except Exception as e:
            logger.error(f"[Voice Engine] Streaming synthesis failed: {e}", exc_info=True)
            return

        # Cache the fully-rendered line for instant replay next time.
        if chunks:
            MisoAudioEngine._cache[key] = b"".join(chunks)
            MisoAudioEngine._cache.move_to_end(key)
            while len(MisoAudioEngine._cache) > self._CACHE_MAX:
                MisoAudioEngine._cache.popitem(last=False)


miso_voice = MisoAudioEngine()