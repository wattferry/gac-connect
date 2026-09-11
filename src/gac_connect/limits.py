"""Request limits for the vehicle service.

Every request this library sends passes :data:`DEFAULT_LIMITER`, which all clients
in a process share and which cannot be bypassed; a client may add a second
limiter on top (for example one kept in a file), never replace it. Each request
holds the limiter from dispatch until its response is read, so requests go out
one at a time, even across threads and event loops; the next starts at least
``min_gap`` seconds after the previous finished. Just before a request is sent,
the rolling budgets (and the command budgets for vehicle commands) and any pause
set by a 429 answer are checked; if one is exhausted, RateLimitedError is raised
and nothing is sent. Each request is counted at the time it finished.

The shared limiter keeps its state in a file (``default_state_path()``: the user's
cache folder, or ``$GAC_CONNECT_LIMITS``), locked while each request runs, so
every program and every run on the machine shares one budget and one pause — a
script that is restarted in a loop is limited like one that keeps running. If the
file cannot be written, nothing is sent. A host that persists the state itself
(the Home Assistant integration, through its own storage) sets ``state_path`` to
None before its first request and uses :meth:`Limiter.export_state` /
:meth:`Limiter.import_state` and ``on_change`` instead.

Scope: requests this library sends on one machine. Other machines, the official
app, and the push channel's own broker connection are not counted.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import tempfile
import threading
import time
from bisect import bisect_right, insort
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .const import (
    COMMAND_BUDGETS,
    HTTP_TIMEOUT,
    MAX_RATE_LIMIT_COOLDOWN,
    MIN_REQUEST_GAP,
    RATE_LIMIT_COOLDOWN,
    RECOVERY_PAUSE,
    REQUEST_BUDGETS,
)
from .errors import RateLimitedError

try:  # cross-process locking for state files
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

_LOGGER = logging.getLogger(__name__)
Budgets = tuple[tuple[float, int], ...]
_POLL = 0.02   # seconds between tries for a lock held by another thread or process


def parse_retry_after(value: str | None, now: float | None = None) -> float:
    """Seconds to pause after a 429: Retry-After as seconds or an HTTP date, never
    less than RATE_LIMIT_COOLDOWN nor more than MAX_RATE_LIMIT_COOLDOWN."""
    seconds = 0.0
    if value:
        value = value.strip()
        if value.isdigit():
            seconds = float(value)
        else:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                seconds = when.timestamp() - (time.time() if now is None else now)
            except (TypeError, ValueError, IndexError, OverflowError):
                seconds = 0.0
    if not math.isfinite(seconds):
        seconds = MAX_RATE_LIMIT_COOLDOWN
    return min(max(seconds, RATE_LIMIT_COOLDOWN), MAX_RATE_LIMIT_COOLDOWN)


class Limiter:
    def __init__(
        self,
        *,
        min_gap: float = MIN_REQUEST_GAP,
        request_budgets: Budgets = REQUEST_BUDGETS,
        command_budgets: Budgets = COMMAND_BUDGETS,
        state_path: str | Path | None = None,
    ) -> None:
        self.min_gap = min_gap
        self.request_budgets = request_budgets
        self.command_budgets = command_budgets
        self.state_path = Path(state_path) if state_path else None
        # Called with urgent=True after a 429, else False, whenever state changes.
        self.on_change: Callable[[bool], None] | None = None
        self._mutex = threading.Lock()          # held for a whole request, any thread/loop
        self._sent: deque[float] = deque()      # monotonic finish times, oldest first
        self._commands: deque[float] = deque()
        self._blocked_until = 0.0
        self._last_done = -math.inf
        self._last_warning = -math.inf

    # ---- the one way to reach the service ---------------------------------
    @asynccontextmanager
    async def slot(self, kind: str = "request") -> AsyncIterator[None]:
        """Hold the service for one request (``kind`` "request" or "command").

        Raises RateLimitedError, without sending, if a budget or pause forbids it.
        """
        while not self._mutex.acquire(blocking=False):
            await asyncio.sleep(_POLL)
        try:
            fd = await self._lock_file()
            try:
                self._load()
                gap = self.min_gap - (time.monotonic() - self._last_done)
                if gap > 0:
                    await asyncio.sleep(gap)
                now = time.monotonic()
                self._check(now, kind)
                # Reserve at the latest time this request could finish, so an interrupted
                # run (or a failed final save) is still counted conservatively; the
                # reservation moves to the real finish time below.
                pending = now + HTTP_TIMEOUT
                insort(self._sent, pending)
                if kind == "command":
                    insort(self._commands, pending)
                self._last_done = max(self._last_done, pending)
                self._save(strict=True)        # never send what could not be recorded
                try:
                    yield
                finally:
                    done = time.monotonic()
                    self._settle(self._sent, pending, done)
                    if kind == "command":
                        self._settle(self._commands, pending, done)
                    self._last_done = done
                    self._save(strict=False)
                    self._changed(False)
            finally:
                self._unlock_file(fd)
        finally:
            self._mutex.release()

    @staticmethod
    def _settle(stamps: deque[float], pending: float, done: float) -> None:
        try:
            stamps.remove(pending)
        except ValueError:
            pass
        insort(stamps, done)

    def hold(self, seconds: float, reason: str) -> None:
        """Send nothing for ``seconds`` (a conservative pause, e.g. after lost state)."""
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)
        self.warn("pausing requests for %.0f s: %s", seconds, reason)

    def block(self, seconds: float) -> float:
        """The service answered 429: send nothing for ``seconds``. Returns the pause in force."""
        until = max(self._blocked_until, time.monotonic() + seconds)
        self._blocked_until = until
        self._save(strict=False)
        self._changed(True)
        remaining = until - time.monotonic()
        self.warn("the service answered 429; pausing all requests for %.0f s", remaining)
        return remaining

    def warn(self, msg: str, *args: Any) -> None:
        """Log a warning at most once a minute."""
        if time.monotonic() - self._last_warning >= 60:
            self._last_warning = time.monotonic()
            _LOGGER.warning(msg, *args)

    # ---- state beyond the process ---------------------------------------------
    def export_state(self) -> dict[str, Any]:
        """The limiter's state as wall-clock times (for storing elsewhere)."""
        wall, mono = time.time(), time.monotonic()
        return {
            "sent": [wall - (mono - m) for m in self._sent],
            "commands": [wall - (mono - m) for m in self._commands],
            "blocked_until": wall + (self._blocked_until - mono) if self._blocked_until > mono else 0,
            "last_done": wall - (mono - self._last_done) if math.isfinite(self._last_done) else 0,
        }

    def import_state(self, data: Any) -> bool:
        """Merge stored state, keeping every stored request; returns False (and changes
        nothing) if ``data`` is not a valid state."""
        if not _valid_state(data):
            return False
        wall, mono = time.time(), time.monotonic()

        def to_mono(w: float) -> float:
            return mono - (wall - min(w, wall))     # a time in the future counts as now

        for key, stamps in (("sent", self._sent), ("commands", self._commands)):
            merged = sorted([*stamps, *(to_mono(w) for w in data[key] if w > 0)])
            stamps.clear()
            stamps.extend(merged)
        b = data["blocked_until"]
        if b > wall:
            self._blocked_until = max(self._blocked_until, mono + min(b - wall, MAX_RATE_LIMIT_COOLDOWN))
        if data["last_done"] > 0:
            self._last_done = max(self._last_done, to_mono(data["last_done"]))
        return True

    def _changed(self, urgent: bool) -> None:
        if self.on_change is not None:
            try:
                self.on_change(urgent)
            except Exception:  # noqa: BLE001 — a persistence hook must not break requests
                _LOGGER.exception("request-limit state hook failed")

    # ---- budgets -------------------------------------------------------------
    def _check(self, now: float, kind: str) -> None:
        longest = max(w for w, _ in (*self.request_budgets, *self.command_budgets))
        for stamps in (self._sent, self._commands):
            while stamps and stamps[0] <= now - longest:
                stamps.popleft()
        waits: list[tuple[float, str]] = []
        if now < self._blocked_until:
            waits.append((self._blocked_until - now, "the service asked us to slow down"))
        waits += self._over(self._sent, self.request_budgets, now, "request")
        if kind == "command":
            waits += self._over(self._commands, self.command_budgets, now, "command")
        if waits:
            wait, reason = max(waits)
            wait = max(1.0, wait)
            self.warn("not sending: %s; next request allowed in %.0f s", reason, wait)
            raise RateLimitedError(f"not sent: {reason}; retry in {wait:.0f} s", retry_after=wait)

    @staticmethod
    def _over(stamps: deque[float], budgets: Budgets, now: float, what: str) -> list[tuple[float, str]]:
        """Waits for each budget already full over the window (now - window, now]."""
        out = []
        for window, limit in budgets:
            recent = len(stamps) - bisect_right(stamps, now - window)
            if recent >= limit:
                wait = stamps[len(stamps) - limit] + window - now
                out.append((wait, f"{what} budget reached ({limit} per {window:g} s)"))
        return out

    # ---- state file -------------------------------------------------------------
    async def _lock_file(self) -> int | None:
        """Take the state file's lock without blocking the event loop."""
        if self.state_path is None:
            return None
        if fcntl is None and msvcrt is None:  # pragma: no cover
            raise RateLimitedError("request limits cannot be shared on this platform; not sent", retry_after=60)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(f"{self.state_path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while True:
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:  # pragma: no cover - Windows
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return fd
                except OSError:
                    await asyncio.sleep(_POLL)
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _unlock_file(fd: int | None) -> None:
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            text = self.state_path.read_text()
        except OSError as exc:
            # Could not read it this time: send nothing and leave the record as it is.
            raise RateLimitedError(f"could not read request limits in {self.state_path}: {exc}; not sent",
                                   retry_after=RATE_LIMIT_COOLDOWN) from exc
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not _valid_state(data):
            # Corrupt, so its history (and any pause in it) is unknown: keep a copy, pause
            # for RECOVERY_PAUSE, and write that pause over the record (atomically; if the
            # write fails the corrupt record stays, so later runs pause again too).
            try:
                self.state_path.with_suffix(".corrupt").write_text(text)
            except OSError:
                pass
            self.hold(RECOVERY_PAUSE, f"the request-limit record {self.state_path} was corrupt; delete it "
                                      f"to reset (a copy is in {self.state_path.with_suffix('.corrupt').name})")
            self._save(strict=False)
            return
        # the file is the shared record (this process's own requests included)
        self._sent.clear()
        self._commands.clear()
        self.import_state(data)

    def _save(self, *, strict: bool) -> None:
        if self.state_path is None:
            return
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.export_state()))
            tmp.chmod(0o600)
            tmp.replace(self.state_path)
        except OSError as exc:
            if strict:
                raise RateLimitedError(f"could not record request limits in {self.state_path}: {exc}; not sent",
                                       retry_after=60) from exc
            self.warn("could not save request limits to %s: %s", self.state_path, exc)


def _valid_state(data: Any) -> bool:
    def num(v: Any) -> bool:
        return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
    return (isinstance(data, dict)
            and all(isinstance(data.get(k), list) and all(num(v) for v in data[k]) for k in ("sent", "commands"))
            and num(data.get("blocked_until")) and num(data.get("last_done")))


def default_state_path() -> Path:
    """Where the shared limiter keeps its state: $GAC_CONNECT_LIMITS, else the user's cache folder."""
    env = os.environ.get("GAC_CONNECT_LIMITS")
    if env:
        return Path(env).expanduser()
    try:
        home = Path.home()
    except (RuntimeError, KeyError):
        user = os.getuid() if hasattr(os, "getuid") else "user"
        return Path(tempfile.gettempdir()) / f"gac-connect-{user}" / "limits.json"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = home / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    return base / "gac-connect" / "limits.json"


# Every client passes this limiter; its state is shared by every run on the machine.
DEFAULT_LIMITER = Limiter(state_path=default_state_path())
