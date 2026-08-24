# services/proactive.py
"""Proactive desktop watchers — Grace reaches out on her own (low battery, disk
almost full, a download finishing, a long work session). Desktop app only
(GRACE_DESKTOP=1); a no-op on the cloud. She *speaks* the alert, so it lands even
when her window is behind others."""
import os
import time
import asyncio
import logging

from core.sse import push_workspace_update, subscriber_count
from services.audio import miso_voice

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60          # seconds between checks
BATTERY_THRESHOLD = 20       # warn at/below this %
DISK_FREE_GB = 5.0           # warn when free space drops below this


class ProactiveMonitor:
    def __init__(self):
        self._task = None
        self._battery_warned = False
        self._disk_warned = False
        self._known_downloads = None
        self._last_break = time.time()
        # per-watcher toggles + break interval (hours)
        self.settings = {"battery": True, "disk": True, "downloads": True, "break": True}
        self.break_hours = 2.0

    def enabled(self) -> bool:
        return os.environ.get("GRACE_DESKTOP", "") == "1"

    async def start(self):
        if not self.enabled():
            return
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            logger.info("[Proactive] watchers started")

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self):
        await asyncio.sleep(15)
        self._known_downloads = self._download_names()   # snapshot: ignore pre-existing
        while True:
            try:
                if subscriber_count() > 0:
                    await self._check()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[Proactive] check failed: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _is_temp(self, f: str) -> bool:
        return f.lower().endswith((".crdownload", ".tmp", ".part", ".partial", ".download"))

    def _download_names(self):
        try:
            from services import os_control
            d = os_control.expand("Downloads")
            if os.path.isdir(d):
                return {f for f in os.listdir(d) if not self._is_temp(f)}
        except Exception:
            pass
        return set()

    async def _say(self, message: str):
        audio_url = ""
        try:
            audio_url = await miso_voice.generate_speech(message)
        except Exception as e:
            logger.warning(f"[Proactive] TTS failed: {e}")
        await push_workspace_update(
            target_widget="WIDGET_CHAT",
            payload={"message": message},
            modality="hybrid" if audio_url else "widget_only",
            audio_url=audio_url,
        )

    # ── the checks ──────────────────────────────────────────────────────────
    async def _check(self):
        import psutil
        s = self.settings

        if s.get("battery"):
            try:
                b = psutil.sensors_battery()
                if b is not None:
                    if (not b.power_plugged) and b.percent <= BATTERY_THRESHOLD and not self._battery_warned:
                        await self._say(f"Heads up, Boss — battery's at {round(b.percent)}%. You might want to plug in.")
                        self._battery_warned = True
                    if b.power_plugged or b.percent > BATTERY_THRESHOLD + 5:
                        self._battery_warned = False
            except Exception:
                pass

        if s.get("disk") and not self._disk_warned:
            try:
                du = psutil.disk_usage(os.path.abspath(os.sep))
                if du.free < DISK_FREE_GB * 1e9 or du.percent >= 95:
                    await self._say(f"Your main drive is nearly full, Boss — about {round(du.free/1e9, 1)} GB left.")
                    self._disk_warned = True
            except Exception:
                pass

        if s.get("downloads") and self._known_downloads is not None:
            now = self._download_names()
            new = [f for f in (now - self._known_downloads) if not self._is_temp(f)]
            self._known_downloads = now
            if len(new) == 1:
                await self._say(f"Your download just finished — {new[0]}.")
            elif len(new) > 1:
                await self._say(f"{len(new)} downloads just finished, Boss.")

        if s.get("break") and (time.time() - self._last_break) >= self.break_hours * 3600:
            self._last_break = time.time()
            await self._say("You've been at it a couple of hours, Boss — maybe stretch and grab some water.")


proactive_monitor = ProactiveMonitor()
