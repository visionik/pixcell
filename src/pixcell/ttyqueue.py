"""TTY output-queue gating via ``TIOCOUTQ`` for :class:`pixcell.live.LiveImage`.

Inspired by zenbu-labs/terminal-browser's ``pixel-kitty`` tty queue check (MIT).

Before encoding and writing a live frame, ``OutputGate`` queries how many
unread bytes are still queued in the terminal's output buffer using
``fcntl.ioctl(fd, termios.TIOCOUTQ, ...)``. When the queue is backed up past a
threshold, ``LiveImage`` holds the pending frame (latest frame wins) instead of
piling frames into a slow or congested tty.

Where ``TIOCOUTQ`` is unavailable (Windows, non-tty file descriptors, headless
test drivers) or returns ``ENOTTY``/``OSError``, gating is a clean no-op. Note
that Linux's PTY driver reports ``0`` for ``TIOCOUTQ`` on PTY slave fds, so on
Linux PTYs the check is effectively a fast no-op while remaining active on
macOS/BSD PTYs, serial ttys, and socket-backed terminals.

Set ``PIXCELL_OUTPUT_GATING=0`` (or pass ``output_gating=False`` to
:func:`pixcell.textual.configure` or :class:`pixcell.live.LiveImage`) to opt
out. ``PIXCELL_OUTPUT_QUEUE_LIMIT`` overrides the byte threshold.
"""

from __future__ import annotations

import io
import os
import struct
from collections.abc import Callable, Mapping

try:
    import fcntl
    import termios

    TIOCOUTQ: int | None = getattr(termios, "TIOCOUTQ", None)
except ImportError:  # Windows or platforms without fcntl/termios
    fcntl = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]
    TIOCOUTQ = None

ENV_VAR = "PIXCELL_OUTPUT_GATING"
"""Set to ``0`` (or ``false``/``no``/``off``) to opt out of output-queue gating."""

LIMIT_ENV_VAR = "PIXCELL_OUTPUT_QUEUE_LIMIT"
"""Optional non-negative integer byte threshold overriding :data:`DEFAULT_THRESHOLD`."""

DEFAULT_THRESHOLD = 4096
"""Queued tty output bytes above which a live frame is held for the next tick."""

_OPT_OUT_VALUES = frozenset({"0", "false", "no", "off"})
_ZERO_INT = struct.pack("i", 0)


def available() -> bool:
    """Is ``TIOCOUTQ`` ioctl supported on this platform?"""
    return fcntl is not None and TIOCOUTQ is not None


def opted_out(environ: Mapping[str, str] | None = None) -> bool:
    """Has the user opted out of output-queue gating via ``PIXCELL_OUTPUT_GATING``?"""
    env = os.environ if environ is None else environ
    return env.get(ENV_VAR, "").strip().lower() in _OPT_OUT_VALUES


def threshold_from_env(
    environ: Mapping[str, str] | None = None,
    default: int = DEFAULT_THRESHOLD,
) -> int:
    """Resolve the byte threshold from ``PIXCELL_OUTPUT_QUEUE_LIMIT`` or ``default``."""
    env = os.environ if environ is None else environ
    raw = env.get(LIMIT_ENV_VAR, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            pass
        else:
            if value >= 0:
                return value
    return default


def queued_bytes(
    fd: int | None,
    *,
    ioctl_fn: Callable[[int, int, bytes], bytes] | None = None,
) -> int | None:
    """Unread bytes queued on ``fd`` via ``TIOCOUTQ``, or None if unavailable.

    Never raises: returns None when ``TIOCOUTQ`` is unsupported or ``fd`` does
    not support the ioctl (e.g. pipes, regular files, closed fds, or Windows).
    """
    if fd is None or fd < 0:
        return None
    if ioctl_fn is None:
        if fcntl is None or TIOCOUTQ is None:
            return None
        ioctl_fn = fcntl.ioctl
        request = TIOCOUTQ
    else:
        request = TIOCOUTQ if TIOCOUTQ is not None else 0x5411
    try:
        result = ioctl_fn(fd, request, _ZERO_INT)
        value = struct.unpack("i", result)[0]
    except (OSError, ValueError, TypeError, struct.error):
        return None
    return max(0, int(value))


def driver_fd(driver: object) -> int | None:
    """Best-effort output file descriptor for a Textual driver, or None."""
    if driver is None:
        return None
    writer = getattr(driver, "_writer_thread", None)
    for stream in (getattr(writer, "_file", None), getattr(driver, "_file", None)):
        if stream is None:
            continue
        fileno = getattr(stream, "fileno", None)
        if callable(fileno):
            try:
                fd = int(fileno())
            except (OSError, ValueError, TypeError, io.UnsupportedOperation):
                continue
            if fd >= 0:
                return fd
    direct = getattr(driver, "fileno", None)
    if isinstance(direct, int) and not isinstance(direct, bool) and direct >= 0:
        return direct
    if callable(direct):
        try:
            fd = int(direct())
        except (OSError, ValueError, TypeError, io.UnsupportedOperation):
            return None
        return fd if fd >= 0 else None
    return None


class OutputGate:
    """Checks whether the terminal's output queue is backed up past a threshold."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        threshold: int | None = None,
        default_enabled: Callable[[], bool] | None = None,
        ioctl_fn: Callable[[int, int, bytes], bytes] | None = None,
    ) -> None:
        self._enabled = enabled
        self._threshold = threshold
        self._default_enabled = default_enabled
        self._ioctl_fn = ioctl_fn

    def is_enabled(self, environ: Mapping[str, str] | None = None) -> bool:
        """Whether output-queue gating is active right now."""
        if opted_out(environ):
            return False
        if self._enabled is not None:
            return self._enabled
        if self._default_enabled is not None:
            return self._default_enabled()
        return True

    def threshold(self, environ: Mapping[str, str] | None = None) -> int:
        """Effective byte threshold for holding frames."""
        if self._threshold is not None:
            return max(0, self._threshold)
        return threshold_from_env(environ)

    def queued(self, driver: object = None, *, fd: int | None = None) -> int | None:
        """Queued output bytes for ``fd`` or ``driver``, or None if unavailable."""
        target = driver_fd(driver) if fd is None else fd
        return queued_bytes(target, ioctl_fn=self._ioctl_fn)

    def is_backed_up(
        self,
        driver: object = None,
        *,
        fd: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> bool:
        """True if gating is enabled and the tty output queue exceeds the threshold."""
        if not self.is_enabled(environ):
            return False
        depth = self.queued(driver, fd=fd)
        if depth is None:
            return False
        return depth > self.threshold(environ)
