"""POSIX shared-memory frame pool for local Kitty graphics (``t=s``).

Inspired by zenbu-labs/terminal-browser's ``pixel-kitty`` SHM pool (MIT),
re-implemented for pixcell using Python's standard library (``_posixshmem`` and
``mmap``).

A :class:`ShmPool` pre-creates and pre-faults POSIX shared-memory segments in
power-of-two size classes on a background thread, so writing a live video or
browser frame copies pixels straight into already-faulted pages without
per-frame ``shm_open``/``ftruncate``/page-fault jitter, zlib compression, or
base64 encoding of pixel data.

Kitty's ``t=s`` transmission expects the terminal emulator to ``shm_unlink``
the segment once it has read the pixels. Each handed-off name is still tracked
so stale segments (from a dropped write, an error, or a terminal that does not
unlink) are unlinked after a short TTL, on :meth:`ShmPool.close` (widget
unmount), and at interpreter exit via :mod:`atexit`.

Only used when the terminal is local (not SSH, not tmux/screen), is not iTerm2
(which takes PNG faster over the direct path), has POSIX SHM available, is
known or probed to support ``t=s``, and ``PIXCELL_SHM`` is not opted out.
"""

from __future__ import annotations

import atexit
import itertools
import mmap
import os
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

try:
    import _posixshmem
except ImportError:  # Windows or platforms without POSIX shared memory
    _posixshmem = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from pixcell.probe import Capabilities

ENV_VAR = "PIXCELL_SHM"
"""Set to ``0`` (or ``false``/``no``/``off``) to opt out of SHM frame transfer."""

MIN_CLASS_BYTES = 64 * 1024
"""Smallest pooled size class (64 KiB)."""

DEFAULT_READY_PER_SIZE = 6
"""Pre-faulted segments kept ready per active size class."""

DEFAULT_MAX_CLASSES = 6
"""Most recent size classes retained in a pool before evicting older ones."""

DEFAULT_STALE_SECONDS = 3.0
"""Age after which a handed-off segment is unlinked as a leak safeguard."""

DEFAULT_MAX_PENDING = 128
"""Maximum handed-off segment names tracked before sweeping the oldest."""

_OPT_OUT_VALUES = frozenset({"0", "false", "no", "off"})
_SSH_ENV_VARS = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
_MULTIPLEXER_TERMS = frozenset({"screen", "tmux"})
_PAGE_SIZE = getattr(os, "PAGESIZE", 4096) or 4096

_pool_ids = itertools.count(1)
_probe_ids = itertools.count(1)
_live_lock = threading.Lock()
_live_pools: set[ShmPool] = set()


def available() -> bool:
    """Is POSIX shared memory available on this platform?"""
    return os.name == "posix" and _posixshmem is not None


def opted_out(environ: Mapping[str, str] | None = None) -> bool:
    """Has the user opted out of SHM via ``PIXCELL_SHM``?"""
    env = os.environ if environ is None else environ
    return env.get(ENV_VAR, "").strip().lower() in _OPT_OUT_VALUES


def is_ssh(environ: Mapping[str, str] | None = None) -> bool:
    """Does the environment indicate a remote SSH session?"""
    env = os.environ if environ is None else environ
    return any(bool(env.get(var)) for var in _SSH_ENV_VARS)


def is_multiplexer(environ: Mapping[str, str] | None = None, *, tmux: bool = False) -> bool:
    """Does the environment indicate tmux or GNU screen?"""
    if tmux:
        return True
    env = os.environ if environ is None else environ
    if env.get("TMUX") or env.get("STY"):
        return True
    term = env.get("TERM", "").strip().lower()
    if not term:
        return False
    base = term.split("-", 1)[0].split(".", 1)[0]
    return base in _MULTIPLEXER_TERMS


def is_local(environ: Mapping[str, str] | None = None, *, tmux: bool = False) -> bool:
    """True only when running locally (not SSH and not inside tmux/screen)."""
    return not is_ssh(environ) and not is_multiplexer(environ, tmux=tmux)


def can_use_shm(
    caps: Capabilities,
    *,
    environ: Mapping[str, str] | None = None,
    enabled: bool = True,
) -> bool:
    """Decide whether local ``t=s`` SHM transmission should be used for ``LiveImage``.

    Returns True only when:
    - SHM is enabled (neither opted out by config/flag nor ``PIXCELL_SHM=0``),
    - POSIX shared memory is available,
    - the protocol is Kitty placeholders (``kitty``),
    - the session is local (not SSH, not tmux/screen),
    - the terminal is not iTerm2 (iTerm2 falls back to direct PNG), and
    - ``t=s`` is known (``kitty.shm = true``) or passed a live ``t=s`` probe
      (and was not contradicted by ``kitty.shm = false`` or a failed probe).
    """
    if not enabled or opted_out(environ) or not available():
        return False
    if caps.protocol != "kitty":
        return False
    if not is_local(environ, tmux=caps.tmux):
        return False
    if caps.terminal == "iterm2" or caps.frame_format == "png":
        return False
    known = caps.fact("kitty.shm")
    if known is False:
        return False
    if caps.shm_probed is not None:
        return bool(caps.shm_probed)
    return known is True


def size_class(length: int) -> int:
    """Round ``length`` up to a power-of-two size class (at least ``MIN_CLASS_BYTES``)."""
    if length <= 0:
        raise ValueError(f"SHM length must be positive, not {length}")
    if length <= MIN_CLASS_BYTES:
        return MIN_CLASS_BYTES
    return 1 << (length - 1).bit_length()


def _unlink_quiet(name: str) -> bool:
    """Unlink a POSIX SHM name, ignoring ENOENT (already unlinked by the terminal)."""
    if _posixshmem is None:
        return False
    try:
        _posixshmem.shm_unlink(name)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def create_probe_segment(data: bytes = b"\x00\x00\x00") -> str:
    """Create a tiny POSIX SHM object for a one-shot ``a=q,t=s`` probe.

    Caller must unlink the returned name (e.g. via :func:`unlink_probe_segment`)
    in a ``finally`` block in case the terminal did not unlink it.
    """
    if not available():
        raise OSError("POSIX shared memory is not available")
    # Keep names <= 30 chars for macOS PSHMNAMLEN (31 bytes including leading '/').
    name = f"/pxc-{os.getpid() % 0xFFFFF:x}-q-{next(_probe_ids):x}"
    seg = _create_segment(name, len(data), prefault=False)
    try:
        seg.mapping[: len(data)] = data
    except Exception:
        seg.destroy()
        raise
    seg.close_mapping()
    return name


def unlink_probe_segment(name: str) -> bool:
    """Clean up a probe SHM segment (no-op if the terminal already unlinked it)."""
    return _unlink_quiet(name)


@dataclass
class Segment:
    """One pre-faulted POSIX shared-memory mapping owned by a :class:`ShmPool`."""

    name: str
    size: int
    mapping: mmap.mmap | None

    def write(self, data: bytes | bytearray | memoryview) -> int:
        """Copy ``data`` to the start of this segment and return bytes written."""
        if self.mapping is None:
            raise ValueError("segment mapping is closed")
        length = len(data)
        if length > self.size:
            raise ValueError(f"payload ({length} bytes) exceeds segment size ({self.size} bytes)")
        self.mapping[:length] = data
        return length

    def close_mapping(self) -> None:
        """Unmap from this process without unlinking the POSIX SHM name."""
        if self.mapping is not None:
            try:
                self.mapping.close()
            finally:
                self.mapping = None

    def destroy(self) -> bool:
        """Unmap and unlink the POSIX SHM object."""
        self.close_mapping()
        return _unlink_quiet(self.name)


def _create_segment(name: str, length: int, *, prefault: bool = True) -> Segment:
    if _posixshmem is None:
        raise OSError("POSIX shared memory is not available")
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    try:
        fd = _posixshmem.shm_open(name, flags, 0o600)
    except FileExistsError:
        _unlink_quiet(name)
        fd = _posixshmem.shm_open(name, flags, 0o600)
    mapping: mmap.mmap | None = None
    try:
        os.ftruncate(fd, length)
        mapping = mmap.mmap(fd, length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
    except Exception:
        if mapping is not None:
            mapping.close()
        os.close(fd)
        _unlink_quiet(name)
        raise
    os.close(fd)
    if prefault:
        try:
            view = memoryview(mapping)
            try:
                page_count = (length + _PAGE_SIZE - 1) // _PAGE_SIZE
                view[::_PAGE_SIZE] = bytes(page_count)
            finally:
                view.release()
        except Exception:
            mapping.close()
            _unlink_quiet(name)
            raise
    return Segment(name=name, size=length, mapping=mapping)


class ShmPool:
    """Pre-faulted, size-classed POSIX shared-memory pool for ``t=s`` frames.

    Segments are pre-faulted per power-of-two size class so hot-path frame
    writes reuse already-mapped memory instead of calling ``shm_open``,
    ``ftruncate``, and faulting pages on the UI thread. Unused segments and
    stale handed-off names are unlinked on :meth:`sweep`, :meth:`close`, error
    paths, and interpreter exit.
    """

    def __init__(
        self,
        *,
        ready_per_size: int = DEFAULT_READY_PER_SIZE,
        max_classes: int = DEFAULT_MAX_CLASSES,
        stale_after: float = DEFAULT_STALE_SECONDS,
        max_pending: int = DEFAULT_MAX_PENDING,
        background_refill: bool = True,
    ) -> None:
        if not available():
            raise OSError("POSIX shared memory is not available")
        self._pool_id = next(_pool_ids)
        self._pid_tag = f"{os.getpid() % 0xFFFFF:x}"
        self._seq = itertools.count(1)
        self._ready_per_size = max(1, ready_per_size)
        self._max_classes = max(1, max_classes)
        self._stale_after = max(0.0, stale_after)
        self._max_pending = max(1, max_pending)
        self._background_refill = background_refill

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._ready: dict[int, deque[Segment]] = {}
        self._recent: deque[int] = deque()
        self._wanted: deque[int] = deque()
        self._pending: deque[tuple[float, str]] = deque()
        self._worker: threading.Thread | None = None
        self._closed = False

        # Counters for diagnostics and tests.
        self.hits = 0
        self.misses = 0
        self.created = 0
        self.unlinked = 0

        with _live_lock:
            _live_pools.add(self)

    def _next_name(self) -> str:
        # <= 30 chars for macOS PSHMNAMLEN.
        return f"/pxc-{self._pid_tag}-{self._pool_id:x}-{next(self._seq):x}"

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def ready_count(self) -> int:
        with self._lock:
            return sum(len(bucket) for bucket in self._ready.values())

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _ensure_worker_locked(self) -> None:
        if not self._background_refill or self._closed or self._worker is not None:
            return
        thread = threading.Thread(
            target=self._refill_loop,
            name=f"pixcell-shm-{self._pool_id}",
            daemon=True,
        )
        self._worker = thread
        thread.start()

    def _request_refill_locked(self, cls: int) -> None:
        if self._closed or not self._background_refill:
            return
        if cls not in self._wanted:
            self._wanted.append(cls)
        self._ensure_worker_locked()
        self._cond.notify()

    def _record_class_locked(self, cls: int) -> list[Segment]:
        """Track ``cls`` as most-recently used; return segments from any evicted class."""
        try:
            self._recent.remove(cls)
        except ValueError:
            pass
        self._recent.append(cls)
        evicted: list[Segment] = []
        while len(self._recent) > self._max_classes:
            old_cls = self._recent.popleft()
            bucket = self._ready.pop(old_cls, None)
            if bucket:
                evicted.extend(bucket)
        return evicted

    def _refill_loop(self) -> None:
        while True:
            with self._cond:
                while not self._closed and not self._wanted:
                    self._cond.wait()
                if self._closed:
                    return
                cls = self._wanted.popleft()
                evicted = self._record_class_locked(cls)
            for seg in evicted:
                if seg.destroy():
                    self.unlinked += 1

            while True:
                with self._lock:
                    if self._closed:
                        return
                    have = len(self._ready.get(cls, ()))
                    if have >= self._ready_per_size:
                        break
                    name = self._next_name()
                try:
                    seg = _create_segment(name, cls, prefault=True)
                except OSError:
                    break
                discard = False
                with self._lock:
                    self.created += 1
                    if self._closed or len(self._ready.get(cls, ())) >= self._ready_per_size:
                        discard = True
                    else:
                        self._ready.setdefault(cls, deque()).append(seg)
                if discard:
                    if seg.destroy():
                        self.unlinked += 1
                    break

    def warm(self, length: int, count: int | None = None) -> int:
        """Synchronously pre-fault up to ``count`` segments for ``length``'s size class."""
        cls = size_class(length)
        target = self._ready_per_size if count is None else max(0, min(count, self._ready_per_size))
        added = 0
        with self._lock:
            if self._closed:
                return 0
            evicted = self._record_class_locked(cls)
        for seg in evicted:
            if seg.destroy():
                self.unlinked += 1
        while True:
            with self._lock:
                if self._closed:
                    break
                if len(self._ready.get(cls, ())) >= target:
                    break
                name = self._next_name()
            seg = _create_segment(name, cls, prefault=True)
            discard = False
            with self._lock:
                self.created += 1
                if self._closed or len(self._ready.get(cls, ())) >= target:
                    discard = True
                else:
                    self._ready.setdefault(cls, deque()).append(seg)
                    added += 1
            if discard:
                if seg.destroy():
                    self.unlinked += 1
                break
        return added

    def acquire(self, length: int) -> Segment:
        """Obtain a pre-faulted segment of at least ``length`` bytes."""
        cls = size_class(length)
        seg: Segment | None = None
        evicted: list[Segment] = []
        with self._lock:
            if self._closed:
                raise RuntimeError("ShmPool is closed")
            evicted = self._record_class_locked(cls)
            bucket = self._ready.get(cls)
            if bucket:
                seg = bucket.popleft()
                self.hits += 1
            else:
                self.misses += 1
                name = self._next_name()
            self._request_refill_locked(cls)
        for old in evicted:
            if old.destroy():
                self.unlinked += 1
        if seg is not None:
            return seg
        seg = _create_segment(name, cls, prefault=True)
        with self._lock:
            self.created += 1
        return seg

    def release(self, segment: Segment) -> bool:
        """Return an un-sent segment to the pool for reuse (or destroy it if full/closed)."""
        if segment.mapping is None:
            if segment.destroy():
                with self._lock:
                    self.unlinked += 1
            return False
        reused = False
        with self._lock:
            if not self._closed:
                bucket = self._ready.setdefault(segment.size, deque())
                if len(bucket) < self._ready_per_size:
                    bucket.append(segment)
                    reused = True
        if not reused:
            if segment.destroy():
                with self._lock:
                    self.unlinked += 1
        return reused

    def handoff(self, segment: Segment) -> str:
        """Unmap ``segment`` locally and track its name for stale-segment cleanup.

        Call immediately before sending the Kitty ``t=s`` escape sequence naming
        ``segment.name``.
        """
        segment.close_mapping()
        now = time.monotonic()
        self.sweep(now=now)
        with self._lock:
            if self._closed:
                if _unlink_quiet(segment.name):
                    self.unlinked += 1
                raise RuntimeError("ShmPool is closed")
            self._pending.append((now, segment.name))
        return segment.name

    def write(self, data: bytes | bytearray | memoryview) -> str:
        """Write ``data`` into a pooled segment and hand it off for ``t=s`` sending.

        If copying fails, the segment is closed and unlinked before re-raising.
        """
        length = len(data)
        if length <= 0:
            raise ValueError("cannot write empty SHM payload")
        segment = self.acquire(length)
        try:
            segment.write(data)
            return self.handoff(segment)
        except Exception:
            if segment.destroy():
                with self._lock:
                    self.unlinked += 1
            raise

    def sweep(self, *, now: float | None = None) -> int:
        """Unlink handed-off segments older than ``stale_after`` or past ``max_pending``."""
        clock = time.monotonic() if now is None else now
        expired: list[str] = []
        with self._lock:
            while self._pending:
                created_at, name = self._pending[0]
                if (clock - created_at) < self._stale_after and len(self._pending) <= self._max_pending:
                    break
                self._pending.popleft()
                expired.append(name)
        removed = 0
        for name in expired:
            if _unlink_quiet(name):
                removed += 1
        if removed:
            with self._lock:
                self.unlinked += removed
        return removed

    def close(self) -> None:
        """Stop refilling and unlink all pooled and tracked SHM segments."""
        ready_segments: list[Segment] = []
        pending_names: list[str] = []
        worker: threading.Thread | None = None
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._wanted.clear()
            for bucket in self._ready.values():
                ready_segments.extend(bucket)
            self._ready.clear()
            self._recent.clear()
            while self._pending:
                pending_names.append(self._pending.popleft()[1])
            worker = self._worker
            self._worker = None
            self._cond.notify_all()

        with _live_lock:
            _live_pools.discard(self)

        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)

        removed = 0
        for seg in ready_segments:
            if seg.destroy():
                removed += 1
        for name in pending_names:
            if _unlink_quiet(name):
                removed += 1
        if removed:
            with self._lock:
                self.unlinked += removed

    def __enter__(self) -> ShmPool:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _cleanup_all() -> None:
    """atexit hook: close and unlink any ShmPool still open at interpreter exit."""
    with _live_lock:
        pools = list(_live_pools)
    for pool in pools:
        try:
            pool.close()
        except Exception:
            pass


atexit.register(_cleanup_all)
